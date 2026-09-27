"""«Contar algo»: una nota del dueño sobre un asunto, en sus palabras.

La nota siempre queda guardada como hito del asunto (`mod_asuntos_notas`), aunque el modelo no
esté o falle: es la palabra del dueño y no depende de nada. Encima de eso, un intérprete barato
(consumer `asuntos_nota`, DeepSeek por default) lee la nota junto con los pasos pendientes y
devuelve qué cambia: fecha a un paso («el viernes en la tarde voy a la sede»), un paso hecho («ya
pagué»), un plazo real («tengo hasta el 30») o un estado nuevo. Lo que devuelve se aplica de
forma DETERMINISTA y acotada: solo ids de pasos que existen y están pendientes, fechas dentro de
una ventana razonable, nada que la nota no diga. Cada cambio queda escrito en la nota (`cambios`)
para que se vea qué hizo.

Sirve igual desde la web (`POST /asuntos/{id}/nota`), la consola y Telegram (`memex asuntos
nota`). Prompt por categorías, sin marcas ni casos del dueño.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.localtime import LOCAL_TZ
from memex.core.observability import record_llm_call
from memex.db import connection
from memex.llm import ChatMessage, LLMClient
from memex.llm.registry import aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.modules.asuntos import store
from memex.modules.tareas import module as tareas

_log = get_logger("memex.modules.asuntos.notas")

PURPOSE = "asuntos_nota"
MAX_TEXTO = 2000
_MAX_TOKENS = 400
#: Una fecha que la nota le pone a un paso tiene que caer cerca de hoy: atrás («ya fui el
#: lunes») o adelante, pero no en otro año. Fuera de esto es un error del modelo, no un dato.
_DIAS_ATRAS = 30
_DIAS_ADELANTE = 365

_DIAS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_MESES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")

NOTA_SYSTEM_PROMPT = (
    "Sos el asistente de un ASUNTO personal (un trámite, un curso, un proyecto, un evento, un\n"
    "servicio). La persona te acaba de CONTAR algo sobre él, en sus palabras. Tu trabajo es\n"
    "entender qué cambia con eso y devolverlo en JSON; nada más.\n\n"
    "Te doy: el asunto (nombre, descripción, estado vigente), la fecha de hoy con los próximos\n"
    "días, los PASOS PENDIENTES con su id, y la nota.\n\n"
    "Respondé SOLO con JSON:\n"
    '{"entendido": "<una frase: qué entendiste de la nota>",\n'
    ' "pasos": [{"id": <id de un paso pendiente>, "fecha": "YYYY-MM-DD" | null,'
    ' "hecho": true | false}],\n'
    ' "vence": "YYYY-MM-DD" | null,\n'
    ' "estado": "<nuevo estado del asunto, una o dos frases>" | null}\n\n'
    "Reglas:\n"
    "- `pasos`: SOLO ids de la lista de pendientes. Incluí un paso solo si la nota le pone fecha\n"
    "  («el viernes voy a...»), lo da por hecho («ya pagué») o las dos cosas. Si la nota no habla\n"
    "  de ningún paso, [].\n"
    "- `fecha`: el día en que la persona va a hacer ese paso, deducido de la nota y de la\n"
    "  fecha de hoy («el viernes» = el próximo viernes de la lista; «mañana»; «el 25»). Si la\n"
    "  nota deja la fecha abierta («cualquier día») pero nombra UNA preferida, usá la\n"
    "  preferida. Sin fecha en la nota → null. NUNCA inventes una.\n"
    "- `hecho`: true solo si la nota dice que ese paso YA se hizo.\n"
    "- `vence`: solo si la nota da un plazo real («tengo hasta el 30», «vence el lunes»). El día\n"
    "  en que PIENSA ir no es un plazo → null.\n"
    "- `estado`: solo si la nota cambia en qué punto está el asunto (algo se hizo, se confirmó,\n"
    "  se trabó, cambió el camino). Una preferencia de día no cambia el estado → null.\n"
    "- Todo en español, sin datos que la nota no traiga."
)


@dataclass(frozen=True)
class PasoCambio:
    id: int
    fecha: date | None = None
    hecho: bool = False


@dataclass(frozen=True)
class Interpretacion:
    """Lo que el modelo entendió de la nota; `aplicar` decide qué de esto vale."""

    entendido: str = ""
    pasos: tuple[PasoCambio, ...] = ()
    vence: date | None = None
    estado: str | None = None


@dataclass(frozen=True)
class NotaResult:
    nota_id: int
    asunto_id: int
    texto: str
    entendido: str
    cambios: list[str] = field(default_factory=list)
    #: False cuando la nota quedó guardada pero nadie la interpretó (sin proveedor, error).
    interpretada: bool = True


def fecha_corta(d: date) -> str:
    """«viernes 18 sep»: como se lee en la agenda, sin año."""
    return f"{_DIAS[d.weekday()]} {d.day} {_MESES[d.month - 1]}"


def _hoy() -> date:
    return datetime.now(LOCAL_TZ).date()


def calendario(hoy: date, dias: int = 7) -> str:
    """«Hoy es miércoles 2026-09-16. Próximos días: jueves 17 = 2026-09-17, ...». La lista es la
    que vuelve «el viernes» inequívoco: el modelo copia una fecha en vez de calcularla."""
    prox = [hoy + timedelta(days=i) for i in range(1, dias + 1)]
    lista = ", ".join(f"{_DIAS[d.weekday()]} {d.day} = {d.isoformat()}" for d in prox)
    return f"Hoy es {_DIAS[hoy.weekday()]} {hoy.isoformat()}. Próximos días: {lista}."


def pendientes_de(conn: Connection, user_id: int, asunto_id: int) -> list[dict[str, Any]]:
    """Pasos y tareas PENDIENTES del asunto (proyectos incluidos, marcados)."""
    return tareas.list_tareas(conn, user_id, asunto_id=asunto_id, limit=60)


def _render_pendientes(rows: Sequence[dict[str, Any]]) -> str:
    if not rows:
        return "(ninguno)"
    by_parent: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        by_parent.setdefault(r.get("parent_id"), []).append(r)
    ids = {int(r["id"]) for r in rows}
    out: list[str] = []
    for r in rows:
        parent = r.get("parent_id")
        if parent is not None and parent in ids:
            continue  # se lista bajo su proyecto
        due = f" · fecha {r['due_on']}" if r.get("due_on") else ""
        hijos = by_parent.get(int(r["id"]), [])
        tag = " (proyecto)" if hijos else ""
        out.append(f"- id {r['id']}: {r['title']}{tag}{due}")
        for h in hijos:
            hdue = f" · fecha {h['due_on']}" if h.get("due_on") else ""
            out.append(f"    - id {h['id']}: {h['title']}{hdue}")
    return "\n".join(out)


def build_messages(
    *,
    asunto: store.Asunto,
    pendientes: Sequence[dict[str, Any]],
    texto: str,
    hoy: date,
) -> list[ChatMessage]:
    user = (
        f"## ASUNTO\nNombre: {asunto.name}\n"
        f"Descripción: {asunto.description or '(sin descripción)'}\n"
        f"Estado vigente: {asunto.state_text or '(sin estado)'}\n\n"
        f"## FECHA\n{calendario(hoy)}\n\n"
        f"## PASOS PENDIENTES\n{_render_pendientes(pendientes)}\n\n"
        f"## NOTA DE LA PERSONA\n{texto}"
    )
    return [ChatMessage("system", NOTA_SYSTEM_PROMPT), ChatMessage("user", user)]


def _as_date(v: Any) -> date | None:
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        return date.fromisoformat(v.strip()[:10])
    except ValueError:
        return None


def parse_interpretacion(content: str) -> Interpretacion:
    """Tolerante: JSON roto o con forma rara → nada que aplicar (la nota igual se guarda)."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return Interpretacion(entendido="")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        return Interpretacion(entendido="")
    pasos: list[PasoCambio] = []
    for p in data.get("pasos") or []:
        if not isinstance(p, dict):
            continue
        try:
            pid = int(p.get("id") or "")
        except (TypeError, ValueError):
            continue
        pasos.append(PasoCambio(id=pid, fecha=_as_date(p.get("fecha")), hecho=bool(p.get("hecho"))))
    estado = data.get("estado")
    estado_txt = " ".join(str(estado).split())[:600] if isinstance(estado, str) else ""
    return Interpretacion(
        entendido=" ".join(str(data.get("entendido") or "").split())[:300],
        pasos=tuple(pasos),
        vence=_as_date(data.get("vence")),
        estado=estado_txt or None,
    )


def registrar(conn: Connection, user_id: int, asunto_id: int, texto: str) -> int:
    txt = " ".join(texto.split())[:MAX_TEXTO]
    if not txt:
        raise ValueError("la nota está vacía")
    nid = conn.execute(
        text(
            "INSERT INTO mod_asuntos_notas (user_id, asunto_id, texto) "
            "VALUES (:u, :a, :t) RETURNING id"
        ),
        {"u": user_id, "a": asunto_id, "t": txt},
    ).scalar_one()
    return int(nid)


def _guardar_resultado(
    conn: Connection, nota_id: int, *, entendido: str, cambios: Sequence[str]
) -> None:
    conn.execute(
        text(
            "UPDATE mod_asuntos_notas SET entendido = :e, cambios = CAST(:c AS JSONB) WHERE id = :i"
        ),
        {"e": entendido, "c": json.dumps(list(cambios), ensure_ascii=False), "i": nota_id},
    )


def _fecha_valida(d: date, hoy: date) -> bool:
    return hoy - timedelta(days=_DIAS_ATRAS) <= d <= hoy + timedelta(days=_DIAS_ADELANTE)


def aplicar(
    conn: Connection,
    user_id: int,
    asunto_id: int,
    interp: Interpretacion,
    pendientes: Sequence[dict[str, Any]],
    *,
    hoy: date,
) -> list[str]:
    """Aplica lo interpretado con las riendas cortas y devuelve una frase por cambio hecho."""
    por_id = {int(r["id"]): r for r in pendientes}
    cambios: list[str] = []
    for p in interp.pasos:
        row = por_id.get(p.id)
        if row is None:
            _log.info("asuntos.nota.paso_desconocido", asunto_id=asunto_id, tarea_id=p.id)
            continue
        titulo = str(row["title"])
        if p.hecho:
            tareas.set_status(conn, user_id, p.id, tareas.STATUS_COMPLETADA)
            cambios.append(f"Hecho: «{titulo}»")
            continue
        if p.fecha is not None:
            if not _fecha_valida(p.fecha, hoy):
                _log.info(
                    "asuntos.nota.fecha_fuera_de_rango",
                    asunto_id=asunto_id,
                    tarea_id=p.id,
                    fecha=p.fecha.isoformat(),
                )
                continue
            tareas.update(conn, user_id, p.id, due_on=p.fecha)
            cambios.append(f"«{titulo}»: {fecha_corta(p.fecha)}")
    if interp.vence is not None and _fecha_valida(interp.vence, hoy):
        store.set_due(conn, user_id, asunto_id, interp.vence, source="user")
        cambios.append(f"Plazo del asunto: {fecha_corta(interp.vence)}")
    if interp.estado:
        store.record_state(conn, user_id, asunto_id, interp.estado, closed=False, source="manual")
        cambios.append(f"Estado: {interp.estado}")
    return cambios


async def anotar(
    user_id: int,
    asunto_id: int,
    texto: str,
    *,
    llm: LLMClient | None = None,
    interpretar: bool = True,
    today: date | None = None,
) -> NotaResult:
    """Guarda la nota y, si se puede, la interpreta y aplica. `llm` reemplaza al cliente del
    consumer (tests). Con `interpretar=False` solo anota."""
    hoy = today or _hoy()
    with connection() as conn:
        a = store.get_asunto(conn, user_id, asunto_id)
        if a is None:
            raise ValueError(f"asunto {asunto_id} no existe")
        if a.merged_into_id is not None:
            asunto_id = store.resolve_merged(conn, user_id, a.id)
            a = store.get_asunto(conn, user_id, asunto_id) or a
        nota_id = registrar(conn, user_id, asunto_id, texto)
        pendientes = pendientes_de(conn, user_id, asunto_id)
    txt = " ".join(texto.split())[:MAX_TEXTO]
    if not interpretar:
        return NotaResult(nota_id, asunto_id, txt, "", [], interpretada=False)

    msgs = build_messages(asunto=a, pendientes=pendientes, texto=txt, hoy=hoy)
    client = llm
    own = client is None
    try:
        if client is None:
            client = build_llm_client(PURPOSE, user_id=user_id)
        result = await client.complete(
            msgs, response_format="json_object", temperature=0.0, max_tokens=_MAX_TOKENS
        )
    except Exception as exc:  # sin proveedor, cuota, red: la nota ya está guardada
        _log.warning(
            "asuntos.nota.sin_interprete",
            asunto_id=asunto_id,
            nota_id=nota_id,
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
        return NotaResult(nota_id, asunto_id, txt, "", [], interpretada=False)
    finally:
        if own and client is not None:
            await aclose_llm(client)

    interp = parse_interpretacion(result.content)
    record_llm_call(
        user_id=user_id,
        purpose=PURPOSE,
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        status="ok",
        cache_hit_tokens=result.usage.cache_hit_tokens,
        metadata={"asunto_id": asunto_id, "nota_id": nota_id, "pasos": len(interp.pasos)},
    )
    with connection() as conn:
        cambios = aplicar(conn, user_id, asunto_id, interp, pendientes, hoy=hoy)
        _guardar_resultado(conn, nota_id, entendido=interp.entendido, cambios=cambios)
    _log.info(
        "asuntos.nota.aplicada",
        asunto_id=asunto_id,
        nota_id=nota_id,
        cambios=len(cambios),
    )
    return NotaResult(nota_id, asunto_id, txt, interp.entendido, cambios)


def list_notas(conn: Connection, user_id: int, asunto_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT id, texto, entendido, cambios, created_at FROM mod_asuntos_notas "
            "WHERE user_id = :u AND asunto_id = :a ORDER BY id"
        ),
        {"u": user_id, "a": asunto_id},
    ).mappings()
    return [
        {
            "id": int(r["id"]),
            "texto": str(r["texto"]),
            "entendido": str(r["entendido"] or ""),
            "cambios": [str(c) for c in (r["cambios"] or [])],
            "created_at": r["created_at"],
            "fecha_local": r["created_at"].astimezone(LOCAL_TZ).date(),
        }
        for r in rows
    ]


__all__ = [
    "MAX_TEXTO",
    "NOTA_SYSTEM_PROMPT",
    "PURPOSE",
    "Interpretacion",
    "NotaResult",
    "PasoCambio",
    "anotar",
    "aplicar",
    "build_messages",
    "calendario",
    "fecha_corta",
    "list_notas",
    "parse_interpretacion",
    "pendientes_de",
    "registrar",
]
