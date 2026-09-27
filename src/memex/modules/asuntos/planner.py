"""Convierte un objetivo declarado en un plan concreto, con fuentes OFICIALES.

El dueño dice «quiero sacar el pasaporte» y el sistema le contesta qué hacer, en qué orden, dónde,
qué llevar y cuánto cuesta. Es la primera pieza de asuntos que NO nace de un mensaje: los demás
asuntos se infieren del correo y los chats, este lo declara él.

Reglas que lo definen, en orden de importancia:

1. **Fuente oficial o no va.** Un trámite mal explicado hace perder un día de fila. El plan solo
   afirma lo que encontró en la página de quien manda (la entidad del trámite, la universidad, el
   proveedor del servicio); los blogs, agregadores y foros no sirven ni para confirmar. Cada paso
   lleva su URL y lo que no se pudo confirmar sale aparte, dicho como lo que es.
2. **No inventar fechas.** El sistema no puede pedir la cita por él. Un paso lleva fecha solo si la
   FUENTE la da (un plazo que vence, un tiempo de entrega); si no, va sin fecha. Es preferible un
   paso sin fecha a una fecha inventada que después no se cumple.
3. **Codex siempre.** El plan sale de `codex exec` con `web_search_mode=live`, igual que los
   curadores: es lo caro y difícil, y la búsqueda web solo existe por ahí. No pasa por
   `build_llm_client` (esa tabla es para los consumidores de `complete`).

Lo que produce se materializa con lo que ya existe: un proyecto en `mod_tareas` con sus pasos
colgados (`find_or_create_parent` + `add(parent=...)`), y el resumen como estado del asunto. Así el
plan aparece solo en «Qué sigue», en la agenda del día y en el resumen de Telegram, sin superficie
nueva.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Connection

from memex.core.observability import record_llm_call
from memex.db import connection
from memex.geo.places import list_anchors
from memex.logging import get_logger
from memex.modules.asuntos import datos as asuntos_datos
from memex.modules.asuntos import store
from memex.modules.tareas import module as tareas
from memex.webcontext._codex_subprocess import CodexRun, run_codex

_log = get_logger("memex.modules.asuntos.planner")

PURPOSE = "asuntos_plan"
#: Un plan con búsqueda web tarda: el curador con web ya pasaba de 4 minutos con 8 ítems.
_DEFAULT_TIMEOUT_S = 900.0
#: Tope de pasos. Más que esto no es un plan, es un manual: el dueño no lo va a leer.
MAX_PASOS = 12


@dataclass
class Paso:
    """Un paso del plan. `fecha` solo si la FUENTE la da; si no, None (ver regla 2)."""

    titulo: str
    que_hacer: str = ""
    donde: str = ""
    llevar: tuple[str, ...] = ()
    costo: str = ""
    fecha: date | None = None
    fuente: str = ""
    #: Cuando había más de una opción (sede, banco, canal): cuál se eligió y por qué, y las otras
    #: en una línea. Vacío si no había nada que elegir.
    por_que: str = ""


@dataclass
class Plan:
    resumen: str = ""
    pasos: list[Paso] = field(default_factory=list)
    fuentes: tuple[str, ...] = ()
    sin_confirmar: tuple[str, ...] = ()


@dataclass
class PlanResult:
    """Qué quedó en el sistema. `creadas` son pasos nuevos; los repetidos los funde `tareas.add`."""

    asunto_id: int
    plan: Plan
    proyecto_id: int | None = None
    creadas: int = 0
    repetidas: int = 0
    dry_run: bool = False


def _schema() -> dict[str, Any]:
    """Esquema de salida que se le pasa a codex (`--output-schema`).

    `fuente` es obligatoria en cada paso a propósito: obliga al modelo a decir DE DÓNDE sacó lo que
    afirma, y hace visible de un vistazo si se apoyó en un blog.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["resumen", "pasos", "fuentes", "sin_confirmar"],
        "properties": {
            "resumen": {
                "type": "string",
                "description": "Una frase: en qué consiste el trámite y qué se necesita para él.",
            },
            "pasos": {
                "type": "array",
                "maxItems": MAX_PASOS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "titulo",
                        "que_hacer",
                        "donde",
                        "llevar",
                        "costo",
                        "fecha",
                        "fuente",
                        "por_que",
                    ],
                    "properties": {
                        "titulo": {
                            "type": "string",
                            "description": "Acción en imperativo, corta: «Pagar el trámite».",
                        },
                        "que_hacer": {
                            "type": "string",
                            "description": (
                                "Cómo se hace, en concreto y con los datos que hagan falta "
                                "(URL del formulario, banco, horario). Sin relleno."
                            ),
                        },
                        "donde": {
                            "type": "string",
                            "description": (
                                "Dirección exacta, o la URL si es en línea. Vacío si no aplica."
                            ),
                        },
                        "llevar": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Qué hay que llevar, con el detalle que pida la fuente "
                                "(«foto 4x5 fondo blanco», «cédula original»). Vacío si no aplica."
                            ),
                        },
                        "costo": {
                            "type": "string",
                            "description": "Monto con su moneda si la fuente lo dice; vacío si no.",
                        },
                        "fecha": {
                            "type": ["string", "null"],
                            "description": (
                                "YYYY-MM-DD SOLO si la fuente da un plazo real. NUNCA una fecha "
                                "estimada o sugerida: si no hay plazo publicado, null."
                            ),
                        },
                        "fuente": {
                            "type": "string",
                            "description": "URL OFICIAL de donde salió este paso. Obligatoria.",
                        },
                        "por_que": {
                            "type": "string",
                            "description": (
                                "Si en este paso había que ELEGIR entre opciones (sede, banco, "
                                "canal, horario): cuál elegiste, por qué, y las otras en una "
                                "línea. Vacío si no había nada que elegir."
                            ),
                        },
                    },
                },
            },
            "fuentes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "URLs oficiales consultadas.",
            },
            "sin_confirmar": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Lo que NO se pudo confirmar en una fuente oficial, dicho como pregunta "
                    "abierta. Vale más esto que un dato inventado."
                ),
            },
        },
    }


def build_prompt(
    *,
    objetivo: str,
    descripcion: str,
    contexto: str,
    ciudad: str,
    hoy: date,
    anclas: Sequence[tuple[str, str]] = (),
) -> str:
    """El prompt del planificador. Cerrado a fuentes oficiales por requisito del dueño.

    `anclas` = (nombre, dirección) de los lugares de referencia del dueño. Con ellas el plan
    elige la sede por cercanía; sin ellas elige igual, pero dice qué asumió.
    """
    ctx = f"\nLo que el usuario agrega: {contexto}\n" if contexto.strip() else ""
    desc = f"\nDescripción del asunto: {descripcion}\n" if descripcion.strip() else ""
    if anclas:
        lugares = "\nLugares de referencia de la persona:\n" + "".join(
            f"- {n}: {a}\n" for n, a in anclas
        )
    else:
        lugares = (
            "\nNo se sabe dónde vive ni dónde pasa el día la persona: si la elección depende "
            "de eso, elegí igual y decí explícitamente qué asumiste.\n"
        )
    return (
        "Sos el asistente de una persona que quiere resolver un trámite o una gestión y necesita "
        "saber EXACTAMENTE qué hacer.\n\n"
        f"Objetivo: {objetivo}\n"
        f"{desc}{ctx}"
        f"Ciudad de la persona: {ciudad}\n"
        f"{lugares}"
        f"Hoy es {hoy.isoformat()}.\n\n"
        "REGLA QUE MANDA SOBRE TODAS: buscá en la web y usá ÚNICAMENTE fuentes OFICIALES, es "
        "decir la página de quien manda en este trámite (la entidad de gobierno, la universidad, "
        "el consulado, la empresa que presta el servicio). Blogs, agregadores, foros, noticias y "
        "videos NO sirven, ni siquiera para confirmar algo que ya viste. Si un dato solo aparece "
        "en una fuente no oficial, va en `sin_confirmar`, no en un paso.\n\n"
        "Cómo armar el plan:\n"
        "- Pasos en el ORDEN en que hay que hacerlos, del primero al último.\n"
        "- Cada paso dice qué hacer, dónde (dirección exacta o URL) y qué llevar, con el detalle "
        "que exija la fuente. Si la fuente pide «foto 4x5 fondo blanco», eso va tal cual.\n"
        "- Costo con su moneda cuando la fuente lo publique.\n"
        "- FECHA solo si la fuente publica un plazo real (una convocatoria que cierra, un tiempo "
        "de entrega). NO pongas fechas sugeridas ni estimadas: para eso está null. No podés pedir "
        "citas ni turnos por la persona; si hace falta uno, el paso es «pedir la cita».\n"
        "- Si el trámite cambia según la ciudad, resolvé para la ciudad de la persona y decilo.\n"
        "- Lo que no encuentres en fuente oficial va en `sin_confirmar`, escrito como lo que la "
        "persona tendría que averiguar o preguntar.\n"
        "- Nada de consejos genéricos («llegá temprano», «tené paciencia»). Solo lo verificable.\n"
        "- SIEMPRE UNA RECOMENDACIÓN. Si un paso tiene más de una opción (varias sedes, varios "
        "bancos, presencial o en línea, varios horarios), ELEGÍ UNA por la persona, decí por qué "
        "en `por_que` (cercanía a sus lugares de referencia, horario, costo, lo que la fuente "
        "diga) y mencioná las otras en una línea. Nunca dejes la lista de opciones sin elegir: "
        "«acude a cualquiera de estas sedes» no sirve. Si la razón depende de algo que no sabés, "
        "elegí igual y decí qué asumiste y qué cambiaría la elección.\n"
        "- LO QUE LA PERSONA PIDE MANDA SOBRE EL NOMBRE DEL ASUNTO. Si en «lo que el usuario "
        "agrega» apunta a algo concreto (un formulario, una beca, una convocatoria, un enlace "
        "puntual, «quiero participar en esto»), el plan es ESO: los pasos para completar eso, con "
        "lo que ese formulario o convocatoria pide, y nada más. El resto del programa o del "
        "trámite general, a lo sumo, va en una línea del `resumen`. Un plan de nueve pasos sobre "
        "el programa entero cuando la persona quería llenar un formulario es un plan equivocado.\n"
    )


def _binary(binary: Sequence[str] | None) -> list[str]:
    if binary:
        return list(binary)
    resolved = shutil.which("codex")
    if resolved is None:
        raise RuntimeError("binario `codex` no encontrado en PATH (¿npm i -g @openai/codex?)")
    return [resolved]


async def _ask_codex(prompt: str, *, binary: Sequence[str], timeout_s: float) -> CodexRun:
    """codex con búsqueda web EN VIVO y esquema de salida. Sin web no hay plan: el conocimiento
    del modelo sobre un trámite tiene meses y los requisitos cambian."""
    sandbox = os.environ.get("MEMEX_CODEX_SANDBOX", "read-only")
    with tempfile.TemporaryDirectory(prefix="memex-asuntos-planner-") as tmp:
        schema_file = Path(tmp) / "schema.json"
        schema_file.write_text(json.dumps(_schema()), encoding="utf-8")
        extra = ["-c", "web_search_mode=live", "--output-schema", str(schema_file)]
        return await run_codex(
            binary, prompt, extra_args=extra, sandbox=sandbox, timeout_s=timeout_s
        )


def _as_date(raw: Any) -> date | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _strs(raw: Any, *, limit: int = 20) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    out = [str(x).strip() for x in raw if str(x).strip()]
    return tuple(out[:limit])


def parse_plan(raw: str) -> Plan:
    """Texto de codex → `Plan`. Tolerante: un plan a medias sirve, uno roto no debe reventar."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _log.warning("asuntos.plan.json_invalido", chars=len(raw))
        return Plan()
    if not isinstance(data, dict):
        return Plan()
    pasos: list[Paso] = []
    for p in data.get("pasos") or []:
        if not isinstance(p, dict):
            continue
        titulo = str(p.get("titulo") or "").strip()
        if not titulo:
            continue
        pasos.append(
            Paso(
                titulo=titulo[:200],
                que_hacer=str(p.get("que_hacer") or "").strip()[:900],
                donde=str(p.get("donde") or "").strip()[:300],
                llevar=_strs(p.get("llevar")),
                costo=str(p.get("costo") or "").strip()[:80],
                fecha=_as_date(p.get("fecha")),
                fuente=str(p.get("fuente") or "").strip()[:500],
                por_que=str(p.get("por_que") or "").strip()[:500],
            )
        )
        if len(pasos) >= MAX_PASOS:
            break
    return Plan(
        resumen=str(data.get("resumen") or "").strip()[:600],
        pasos=pasos,
        fuentes=_strs(data.get("fuentes")),
        sin_confirmar=_strs(data.get("sin_confirmar")),
    )


def describe(paso: Paso) -> str:
    """La descripción que ve el dueño en la tarea: todo lo del paso, sin tener que abrir nada."""
    partes: list[str] = []
    if paso.que_hacer:
        partes.append(paso.que_hacer)
    if paso.donde:
        partes.append(f"Dónde: {paso.donde}")
    if paso.por_que:
        partes.append(f"Por qué esta opción: {paso.por_que}")
    if paso.llevar:
        partes.append("Llevar: " + ", ".join(paso.llevar))
    if paso.costo:
        partes.append(f"Costo: {paso.costo}")
    if paso.fuente:
        partes.append(f"Fuente: {paso.fuente}")
    return "\n".join(partes)


def apply_plan(
    conn: Connection, user_id: int, asunto_id: int, plan: Plan, *, titulo_proyecto: str
) -> tuple[int | None, int, int]:
    """Materializa el plan: un proyecto con sus pasos + el resumen como estado del asunto.

    Devuelve (proyecto_id, creadas, repetidas). Correr el plan de nuevo no duplica: `tareas.add`
    dedup por título dentro del asunto y devuelve la que ya existía.
    """
    if not plan.pasos:
        return None, 0, 0
    proyecto_id = tareas.find_or_create_parent(
        conn, user_id, title=titulo_proyecto, asunto_id=asunto_id
    )
    creadas = repetidas = 0
    for paso in plan.pasos:
        row = tareas.add(
            conn,
            user_id,
            title=paso.titulo,
            description=describe(paso),
            due_on=paso.fecha,
            asunto=asunto_id,
            parent=proyecto_id,
            origin="manual",
            evidence="paso del plan",
        )
        if row.get("deduped"):
            repetidas += 1
        else:
            creadas += 1
    if plan.resumen:
        store.record_state(conn, user_id, asunto_id, plan.resumen, closed=False, source="manual")
    for url in plan.fuentes[:5]:
        asuntos_datos.add_dato(conn, user_id, asunto_id, label="fuente", value=url, origin="manual")
    return proyecto_id, creadas, repetidas


async def plan_asunto(
    user_id: int,
    asunto_id: int,
    *,
    contexto: str = "",
    ciudad: str = "Bogotá, Colombia",
    binary: Sequence[str] | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    dry_run: bool = False,
    ask: Any | None = None,
    today: date | None = None,
) -> PlanResult:
    """Arma el plan y lo deja como proyecto con pasos. `ask` reemplaza a codex (tests)."""
    with connection() as conn:
        a = store.get_asunto(conn, user_id, asunto_id)
        anclas = [
            (str(x["name"]), str(x["formatted_address"])) for x in list_anchors(conn, user_id)
        ]
    if a is None:
        raise ValueError(f"asunto {asunto_id} no existe")
    hoy = today or date.today()
    prompt = build_prompt(
        objetivo=a.name,
        descripcion=a.description,
        contexto=contexto,
        ciudad=ciudad,
        hoy=hoy,
        anclas=anclas,
    )
    if ask is not None:
        raw = await ask(prompt)
        usage = None
        latency = 0
    else:
        run = await _ask_codex(prompt, binary=_binary(binary), timeout_s=timeout_s)
        raw, usage, latency = run.last_message, run.usage, run.latency_ms
    plan = parse_plan(raw)
    _log.info(
        "asuntos.plan.listo",
        asunto_id=asunto_id,
        pasos=len(plan.pasos),
        fuentes=len(plan.fuentes),
        sin_confirmar=len(plan.sin_confirmar),
        dry_run=dry_run,
    )
    if usage is not None:
        record_llm_call(
            user_id=user_id,
            purpose=PURPOSE,
            model="codex/default",
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            cache_hit_tokens=usage.cached_input_tokens,
            cost_usd=Decimal("0"),  # codex no cobra por token: la cuenta es la suscripcion
            latency_ms=latency,
            status="ok",
            metadata={"asunto_id": asunto_id, "pasos": len(plan.pasos)},
        )
    if dry_run:
        return PlanResult(asunto_id=asunto_id, plan=plan, dry_run=True)
    with connection() as conn:
        proyecto_id, creadas, repetidas = apply_plan(
            conn, user_id, asunto_id, plan, titulo_proyecto=a.name
        )
    return PlanResult(
        asunto_id=asunto_id,
        plan=plan,
        proyecto_id=proyecto_id,
        creadas=creadas,
        repetidas=repetidas,
    )


__all__ = [
    "MAX_PASOS",
    "PURPOSE",
    "Paso",
    "Plan",
    "PlanResult",
    "apply_plan",
    "build_prompt",
    "describe",
    "parse_plan",
    "plan_asunto",
]
