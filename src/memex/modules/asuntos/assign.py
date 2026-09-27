"""Asignador INCREMENTAL de asuntos: adhesión determinista → LLM por lote con el catálogo vigente.

Por lote (ventana del procesamiento) el modelo ve el catálogo (candidatos + abiertos + cerrados
recientes) y los mensajes nuevos, y decide por mensaje: asunto existente | asunto nuevo | ninguno;
además hitos fechados, actualizaciones de estado/cierre y relaciones entre asuntos. Todo lo que
devuelve se asienta por `store` (libro de evidencia, historial de estados); lo que el aplicador no
contempla se registra con razón (nunca se descarta en silencio).

Consumidor LLM: `asuntos_assign` (DeepSeek por defecto; codex = $0 vía el registry).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import record_llm_call
from memex.db import connection
from memex.llm.client import ChatMessage, LLMClient, LLMError, LLMResult
from memex.llm.registry import aclose_llm, build_llm_client
from memex.logging import bound_log_context, get_logger
from memex.modules.asuntos import finance_link, gemelos, store
from memex.modules.asuntos.adhesion import MessageView, adhere, load_messages
from memex.modules.asuntos.settings import AsuntosSettings, get_settings
from memex.modules.chat_context import mode_label
from memex.modules.principios import PRINCIPIOS_GENERALES
from memex.relevance.verdicts import EMAIL_TYPES

_log = get_logger("memex.modules.asuntos.assign")

CONSUMER = "asuntos_assign"
PURPOSE = "asuntos_assign"
#: Tope de salida por lote. Los modelos con razonamiento (deepseek-v4-pro) gastan parte de este
#: presupuesto pensando ANTES de emitir el JSON: con 8000 el contenido llegaba vacío. Si aun así se
#: trunca (`finish_reason == "length"`), el lote se biseca.
_MAX_TOKENS = 24000
#: Las relaciones entre asuntos son caras de deshacer (tejen el grafo) y el modelo tiende a
#: proponerlas por afinidad: piso propio, por encima del de las asignaciones.
_MIN_RELATION_CONFIDENCE = 0.85
_KEY_RX = re.compile(r"^\s*(A\d+|nuevo:\d+)")
_FENCE_RX = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)

SYSTEM_PROMPT = (
    """Eres la memoria personal del usuario. Recibes sus mensajes en ORDEN
CRONOLÓGICO, por lotes, y mantienes un catálogo de ASUNTOS.

"""
    + PRINCIPIOS_GENERALES
    + """
Un ASUNTO es un hilo de vida con principio, hitos y fin en el que el usuario participa o tiene
algo en trámite: un evento al que se registró o fue invitado personalmente, un bootcamp/programa/
hackathon en el que participa, un trámite (grado, visa, crédito, práctica, salud), un curso o
materia que cursa, un proyecto propio o con otros, una comunidad recurrente a la que pertenece
(meetups periódicos), una relación de servicio (un alumno de tutorías; un servidor o dominio que
administra), un viaje ligado a un evento. El mismo asunto recibe señal por varios medios y
remitentes distintos (organizador, plataforma de registro, calendario, newsletter del organizador,
aerolínea del viaje al evento, recibos del viaje).

NO es asunto: newsletters y digests periódicos, marketing, promociones, avisos genéricos de
plataformas, recibos de rutina, renovaciones automáticas exitosas, cobros ordinarios. Si un
mensaje es de un remitente de asunto conocido pero es contenido general (newsletter del
organizador hablando de otra cosa) → asunto null.
TAMPOCO es asunto una invitación, convocatoria o difusión MASIVA (semilleros, charlas, cursos
abiertos, becas, eventos, torneos) a la que el usuario NO se registró, no fue aceptado, no
pagó ni respondió: el asunto nace cuando ÉL actúa (se registra, lo aceptan, paga, responde,
pregunta), no cuando le escriben a todos. Esos mensajes van con asunto null.
Pagos, recibos, facturas, renovaciones y cobros FALLIDOS tampoco son asunto: los registra el
módulo de finanzas. Eso incluye las SUSCRIPCIONES de consumo con pago fallido (software,
streaming, almacenamiento, apps de aprendizaje) aunque el correo amenace con bajar de plan o
perder el acceso: null. Y los recibos de transporte, pedidos, compras digitales y tiendas de
apps o juegos: null. Si el catálogo trae un asunto viejo de pagos de un servicio, NO le adhieras
recibos ni
cobros: responde null igual. Un aviso automático de facturación SIN monto, sin factura y sin
suspensión ya efectiva («cargos pendientes o datos de pago inválidos», «puede que la tarjeta
haya caducado») también es null: no prueba una deuda; si hubo un cobro rechazado real, finanzas
lo registra. Comprar o dar de alta un servicio propio (dominio, servidor, licencia,
suscripción) es una transacción de finanzas, no un asunto, aunque traiga renovación automática
o factura. La ÚNICA excepción es una mora con HECHO concreto: un monto o una factura vencida
con plazo, cobro prejurídico o jurídico, deuda reportada a centrales de riesgo, o un servicio
PROPIO con proyectos adentro ya suspendido o con fecha cierta de suspensión. Eso sí es un
asunto tipo tramite con prioridad alta, UNO por servicio, con `vence` = el plazo, y el sistema
lo cierra solo cuando finanzas registra el pago.

Reglas:
- Prefiere ADHERIR a un asunto existente antes que crear uno nuevo. Crea uno nuevo solo cuando el
  mensaje muestra un hilo con vida propia. Granularidad: un asunto por programa/evento/trámite/
  materia concreto, no por remitente ni por organización. Sesiones, recordatorios y logística de
  un mismo programa van al mismo asunto. Ediciones recurrentes de un meetup (v17, v18…) = UNA
  comunidad. Un hackathon y el programa/aceleradora que le sigue son DOS asuntos relacionados
  (`continua`).
- UNA COSA DEL MUNDO, UN ASUNTO (principio 2): el evento y la beca, el viaje, la inscripción o
  la postulación PARA ese mismo evento son UN solo asunto, no dos. La confirmación, aceptación o
  recordatorio que manda el organizador de algo que ya está en el catálogo (aunque venga con
  otro nombre, en otro idioma o desde otro remitente) se adhiere a ESE asunto como hito («quedó
  inscrito», «lo aceptaron»): NUNCA abre uno nuevo. Antes de proponer `nuevo:N` revisá si el
  catálogo ya tiene un asunto cuya descripción habla de la misma cosa.
- CAMBIO DE NOMBRE: si un mensaje dice que un programa, comunidad, evento o trámite del catálogo
  pasó a llamarse de otra forma («X pasó a ser Y», «ahora somos Y», nueva marca, niveles o
  categorías renombrados), es el MISMO asunto: adhiere al existente y pon el nombre nuevo en
  `nombre` dentro de `actualizaciones` (el anterior queda como alias). NUNCA abras un asunto nuevo
  por un cambio de nombre. Si el nombre no cambió, `nombre` va en null.
- Cada alumno o contacto de tutorías es un asunto «servicio» propio.
- Un servicio o cita RECURRENTE (terapia, consultas, tutorías, controles) es UN asunto por
  relación/proveedor y cada cita es un HITO: nombre SIN fecha («Citas de salud mental en <lugar>»),
  nunca «Cita <lugar> Feb 3 2026». Si ya existe el asunto de esa relación (aunque esté cerrado o
  inactivo en el catálogo), adhiere y reábrelo con el estado.
- nombre: corto y estable (qué es + con quién/dónde + edición si la edición es la identidad, como
  «Hackathon X 2026»); sin fechas de días ni meses.
- Trámites que son requisito de un proceso mayor (examen de idioma, prueba de Estado, paz y salvos →
  grado; curso de inglés → requisito de inglés) son asuntos propios ENLAZADOS con `requisito_de`
  al proceso mayor si existe en el catálogo o en este lote.
- Si un asunto nuevo del lote engloba varios mensajes, usa la misma ref ("nuevo:1") en todos.
- hito: solo si el mensaje marca un momento del asunto (registro, selección, aceptación, inicio,
  entrega, demo day, resultado, pago de matrícula, cita, cambio de estado de un trámite, cierre).
  fecha = del hito en YYYY-MM-DD si se conoce (puede ser futura), si no null. Recordatorios de
  algo ya registrado NO son hito.
- actualizaciones: cuando un mensaje cambia lo que sabemos del ESTADO de un asunto (qué falta, qué
  se cumplió, resultado), escribe el estado vigente en UNA frase concreta; cerrado=true cuando el
  asunto terminó (programa completado, evento pasado sin continuidad, trámite resuelto). Puedes
  cerrar asuntos del catálogo que evidentemente ya pasaron aunque no haya mensaje en este lote.
- ÁNIMO NO ES ESTADO (principio 1): lo que el usuario siente, duda o dice que no quiere («no
  quiero hacerlo», «no sé si…», «me da pena», «quizás») NO es hito ni actualización. El estado
  registra HECHOS y RESULTADOS (hizo, envió, lo aceptaron, se cerró, cambió el plazo). Un mensaje
  que solo trae ánimo va al asunto de su hilo con hito null y sin actualización.
- EL HILO MANDA (principio 3): el asunto de un mensaje es el de la CONVERSACIÓN en la que ocurre
  —a qué responde y de qué venían hablando los mensajes anteriores del lote (mismo chat, mismos
  minutos)—. Nombrar de paso una entidad de OTRO asunto del catálogo (un proyecto, una marca, una
  persona) no cambia el asunto del hilo; los mensajes seguidos de un mismo hilo van al MISMO.
- relaciones: `parte_de` (src es parte de dst), `requisito_de` (src es requisito para dst),
  `continua` (src continúa en dst; p. ej. hackathon → aceleradora que le sigue). SOLO cuando el
  mensaje lo dice explícitamente (una carrera que enumera los requisitos de grado; un programa que
  anuncia la siguiente fase). NUNCA por afinidad temática, misma institución o mismo remitente.
  En duda, no la propongas.
- Los asuntos [CERRADO] o [INACTIVO] del catálogo son contemporáneos de este lote: si un mensaje
  es la sesión, la logística o la continuación de uno de ellos (misma edición del programa, mismo
  trámite), ADHIERE a ese asunto —NUNCA crees «<programa> sesión N» ni una variante— y, si sigue
  vivo, actualízalo con cerrado=false. Un asunto nuevo solo si es otra edición u otro hilo.
- Un asunto [SILENCIADO] lo cerró el dueño a propósito y no quiere volver a verlo: ADHIERE los
  mensajes de ese hilo igual que siempre (por eso sigue en el catálogo: para que no nazca un
  duplicado) pero NO propongas reabrirlo ni le pongas cerrado=false, y NO crees una variante con
  otro nombre. Su estado y su prioridad tampoco se tocan.
- Mensajes marcados «YA ADHERIDO a A<id>»: su asunto ya está decidido por hilo/evento/asunto;
  responde ese mismo asunto y concéntrate en el hito y el estado que aportan (o nada, si son
  recordatorios). EXCEPCIÓN: si el mensaje es rutina (recibo normal, newsletter, aviso genérico)
  responde asunto null y el sistema lo retira de ese asunto.
- prioridad (obligatoria en `nuevos`, opcional en `actualizaciones` cuando cambia): 'alta' =
  plazo cercano, plata o un derecho en juego, requisito de algo mayor, participación activa con
  entregas; 'normal' = hilo vivo sin urgencia; 'baja' = seguimiento pasivo (comunidad,
  suscripción sin problema). `prioridad_motivo`: frase corta con el porqué, tomada del texto.
- vence: la fecha límite (YYYY-MM-DD) del PRÓXIMO paso si el texto la da o la deja calcular:
  plazo para actuar o pagar, fin del programa o curso, fecha del evento, cierre de una
  convocatoria a la que el usuario ya aplicó, o el momento en que algo se elimina, suspende o
  expira. Un plazo RELATIVO («en 48 horas», «dentro de 30 días», «antes del viernes») se
  convierte a fecha sumándolo a la fecha del mensaje (la que encabeza cada mensaje del lote).
  Ponlo aunque ya haya pasado: el sistema decide el cierre («venció sin novedad»); si después
  llega un correo que muestra que el asunto sigue, el plazo se reemplaza o se pone en null. null
  si no hay plazo. En `actualizaciones` ponlo solo cuando cambie (fecha nueva, o null si el
  plazo desapareció).
- semilla `registro` = la persona se INSCRIBIÓ a un evento, programa o curso. Comprar, contratar
  o dar de alta un servicio NO es `registro` (y no es asunto, ver arriba).
- confianza en [0,1]. Incluye TODOS los inbox_id del lote en asignaciones (asunto null si no
  aplica).

CHATS DE GRUPO (mensajes marcados `chat` con «grupo …»): a diferencia del correo, el mensaje NO
va dirigido al usuario: es un miembro hablando en un grupo. El usuario es un LECTOR salvo que
el mensaje diga «LO ESCRIBIÓ EL USUARIO». Reglas:
- EL GRUPO NUNCA ES EL ASUNTO, ni con su nombre: es el lugar donde se habla. Los asuntos son
  las cosas concretas que ahí se planean o se viven: cada hackathon o convocatoria a la que se
  presentan, cada proyecto o producto que construyen, cada postulación, cada viaje. Un mismo
  grupo alimenta VARIOS asuntos a lo largo del tiempo: cada mensaje va al asunto de la
  iniciativa de la que habla (existente en el catálogo o nuevo si es una iniciativa nueva), y
  la coordinación o la charla sin iniciativa concreta va a null. NUNCA crees un asunto con el
  nombre del grupo ni uno que abarque «todo lo del grupo». Esto vale para los tres tipos de
  chat.
- Lo que otro miembro cuenta de sí mismo (su avance, su ascenso, su entregable, su pregunta,
  su pedido de acceso, su asistencia, una felicitación) NO es hito ni estado del usuario:
  responde null. Nunca redactes un estado en primera persona del usuario a partir de lo que
  dijo otro.
- Sí es señal: (a) un mensaje del propio usuario; (b) un mensaje que le habla al usuario por su
  nombre o le responde; (c) un ANUNCIO del organizador que aplica a todos los participantes
  de un programa en el que el usuario participa (sesión, entregable con plazo, resultados,
  cambio de horario): eso sí actualiza el estado del asunto del programa, en tercera persona
  («el programa abre entregables hasta…»), no como acción del usuario.
- En un «chat de comunidad (solo anuncios)» NO crees asuntos nuevos: los eventos anunciados
  ya van al calendario; el asunto solo existe si el usuario mismo se inscribió o participó
  (mensaje suyo, o correo previo en el catálogo). En un «programa en el que el usuario
  participa», adhiere los anuncios del organizador al asunto del programa y responde null a
  la charla de los demás. En un «grupo cercano del usuario» aplica las reglas normales a las
  iniciativas que ahí se planean (el grupo mismo sigue sin ser asunto).

Responde SOLO JSON COMPACTO (una sola línea, sin indentación ni saltos) con esta forma exacta:
{"asignaciones":[{"inbox_id":int,"asunto":"A12"|"nuevo:1"|null,"confianza":0.0-1.0,
                  "hito":{"fecha":"YYYY-MM-DD"|null,"que":"..."}|null}],
 "nuevos":[{"ref":"nuevo:1","nombre":"...","tipo":"evento|programa|hackathon|tramite|proyecto|comunidad|servicio|otro",
            "descripcion":"una frase","entidades":["..."],
            "semilla":"registro|invitacion|seleccion|conversacion|pago|recordatorio|otro",
            "prioridad":"alta|normal|baja","prioridad_motivo":"...","vence":"YYYY-MM-DD"|null}],
 "actualizaciones":[{"asunto":"A12"|"nuevo:1","estado":"...","cerrado":bool,
                     "nombre":"nombre nuevo si cambió"|null,
                     "prioridad":"alta|normal|baja"|null,"prioridad_motivo":"..."|null,
                     "vence":"YYYY-MM-DD"|null}],
 "relaciones":[{"src":"A12"|"nuevo:1","dst":"A3"|"nuevo:2","tipo":"parte_de|requisito_de|continua","confianza":0.0-1.0}]}
"""
)

_SEEDS = frozenset(
    {"registro", "invitacion", "seleccion", "conversacion", "pago", "recordatorio", "otro"}
)


# --- prompt --------------------------------------------------------------------------------- #


def render_catalog(cat: Sequence[store.Asunto], today: date) -> str:
    lines = []
    for a in cat:
        flag = ""
        if a.silenced:
            flag = " [SILENCIADO]"
        elif a.status == "cerrado":
            flag = " [CERRADO]"
        elif a.status == "inactivo":
            flag = " [INACTIVO]"
        elif a.status == "candidato":
            flag = " [candidato]"
        last = a.last_signal_at.date().isoformat() if a.last_signal_at else "?"
        ents = ", ".join(a.entities[:6])
        alias = f" (alias: {', '.join(a.aliases[:3])})" if a.aliases else ""
        lines.append(
            f"- A{a.id}{flag} · {a.name}{alias} ({a.kind}) · {a.description[:140]} · "
            f"entidades: {ents} · msgs={a.evidence_count} · última={last} · "
            f"estado: {a.state_text[:140] or '—'}"
        )
    return "\n".join(lines) if lines else "(vacío)"


def render_batch(msgs: Sequence[MessageView]) -> str:
    parts = []
    for m in msgs:
        marks = [m.kind]
        if m.kind == "chat" and m.chat_title:
            marks.append(f"grupo «{m.chat_title}» · {mode_label(m.chat_mode)}")
        if m.is_self:
            marks.append("LO ESCRIBIÓ EL USUARIO")
        if m.is_reply:
            marks.append("respuesta-en-hilo")
        if m.is_list:
            marks.append("lista-de-correo")
        pre = ""
        if m.reply_quote:
            # Principio 3: el asignador ve a qué responde el mensaje (quién y qué), como el
            # extractor; sin esto «no quiero hacerlo» no tenía hilo.
            pre = f"↩ en respuesta a {m.reply_quote}\n"
        if m.preassigned is not None:
            pre = (
                f"⚠ YA ADHERIDO a A{m.preassigned} (por {m.preassigned_method}): "
                f'no lo reasignes ni lo pongas en null; responde "asunto":"A{m.preassigned}" '
                "y aporta hito/estado si el mensaje los trae.\n"
            )
        parts.append(
            f"### inbox_id={m.id} · {m.occurred_at:%Y-%m-%d %H:%M} · de: {m.sender_label} "
            f"[{', '.join(marks)}]\n{pre}Asunto: {m.subject}\n{m.body}\n"
        )
    return "\n".join(parts)


def build_messages(cat: Sequence[store.Asunto], msgs: Sequence[MessageView]) -> list[ChatMessage]:
    today = max(m.occurred_at for m in msgs).date()
    user = (
        f"Fecha del lote: {today.isoformat()}\n\n## CATÁLOGO DE ASUNTOS VIGENTE\n"
        f"{render_catalog(cat, today)}\n\n## MENSAJES DEL LOTE ({len(msgs)})\n{render_batch(msgs)}"
    )
    return [ChatMessage("system", SYSTEM_PROMPT), ChatMessage("user", user)]


# --- parseo --------------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Assignment:
    inbox_id: int
    asunto: str | None  # "A12" | "nuevo:1" | None
    confidence: float
    hito_date: date | None
    hito_text: str | None


@dataclass(frozen=True)
class NewAsunto:
    ref: str
    name: str
    kind: str
    description: str
    entities: tuple[str, ...]
    seed: str
    priority: str = "normal"
    priority_reason: str = ""
    due_on: date | None = None


@dataclass(frozen=True)
class StateUpdate:
    asunto: str
    state_text: str
    closed: bool
    #: None = el modelo no opinó sobre la prioridad en esta actualización.
    priority: str | None = None
    priority_reason: str = ""
    #: `due_set` = la clave vino en la respuesta (con fecha o null); sin clave no se toca.
    due_on: date | None = None
    due_set: bool = False
    #: Nombre nuevo cuando el mensaje anuncia un cambio de nombre («X pasó a ser Y»); vacío = no
    #: cambió. El anterior queda como alias (`store.rename`).
    new_name: str = ""


@dataclass(frozen=True)
class Relation:
    src: str
    dst: str
    relation_type: str
    confidence: float


@dataclass
class Parsed:
    assignments: list[Assignment] = field(default_factory=list)
    new: list[NewAsunto] = field(default_factory=list)
    updates: list[StateUpdate] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def _key(v: Any) -> str | None:
    """El modelo a veces escribe «A21 · nombre» en vez de «A21»: nos quedamos con el id."""
    if v is None:
        return None
    m = _KEY_RX.match(str(v))
    return m.group(1) if m else None


def _conf(v: Any, default: float = 0.7) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


_PRIORITY_ALIASES = {
    "alta": "alta",
    "high": "alta",
    "urgente": "alta",
    "normal": "normal",
    "media": "normal",
    "medium": "normal",
    "baja": "baja",
    "low": "baja",
}


def _priority(v: Any) -> str | None:
    """alta | normal | baja (tolera inglés y «media»); vacío o raro → None: el caller decide."""
    if v is None:
        return None
    return _PRIORITY_ALIASES.get(str(v).strip().lower())


def _date(v: Any) -> date | None:
    if not v or not isinstance(v, str):
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def parse_response(content: str) -> Parsed:
    """Parseo defensivo: tolera fences, claves con nombre, campos faltantes; anota descartes."""
    out = Parsed()
    raw = _FENCE_RX.sub("", (content or "").strip())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            out.dropped.append("json:invalid")
            return out
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            out.dropped.append("json:invalid")
            return out
    if not isinstance(data, dict):
        out.dropped.append("json:not_object")
        return out
    for nv in data.get("nuevos") or []:
        if not isinstance(nv, dict):
            continue
        ref = _key(nv.get("ref"))
        name = str(nv.get("nombre") or "").strip()
        if not ref or not ref.startswith("nuevo:") or not name:
            out.dropped.append(f"nuevo:sin_ref_o_nombre:{str(nv.get('nombre'))[:40]}")
            continue
        kind = str(nv.get("tipo") or "otro").strip().lower()
        seed = str(nv.get("semilla") or "otro").strip().lower()
        out.new.append(
            NewAsunto(
                ref=ref,
                name=name[:160],
                kind=kind if kind in store.KINDS else "otro",
                description=str(nv.get("descripcion") or "")[:600],
                entities=tuple(str(x)[:80] for x in (nv.get("entidades") or []) if str(x).strip())[
                    :16
                ],
                seed=seed if seed in _SEEDS else "otro",
                priority=_priority(nv.get("prioridad")) or "normal",
                priority_reason=str(nv.get("prioridad_motivo") or "")[:300],
                due_on=_date(nv.get("vence")),
            )
        )
    for asg in data.get("asignaciones") or []:
        if not isinstance(asg, dict):
            continue
        try:
            iid = int(str(asg.get("inbox_id")))
        except (TypeError, ValueError):
            out.dropped.append("asignacion:sin_inbox_id")
            continue
        raw_key = asg.get("asunto")
        key = _key(raw_key)
        if raw_key is not None and key is None:
            out.dropped.append(f"asignacion:clave_invalida:{str(raw_key)[:40]}")
        h = asg.get("hito") if isinstance(asg.get("hito"), dict) else None
        out.assignments.append(
            Assignment(
                inbox_id=iid,
                asunto=key,
                confidence=_conf(asg.get("confianza")),
                hito_date=_date(h.get("fecha")) if h else None,
                hito_text=(str(h.get("que") or "").strip()[:240] or None) if h else None,
            )
        )
    for up in data.get("actualizaciones") or []:
        if not isinstance(up, dict):
            continue
        key = _key(up.get("asunto"))
        if key is None:
            out.dropped.append(f"actualizacion:clave_invalida:{str(up.get('asunto'))[:40]}")
            continue
        out.updates.append(
            StateUpdate(
                asunto=key,
                state_text=str(up.get("estado") or "").strip()[:600],
                closed=bool(up.get("cerrado")),
                priority=_priority(up.get("prioridad")),
                priority_reason=str(up.get("prioridad_motivo") or "")[:300],
                due_on=_date(up.get("vence")),
                due_set="vence" in up,
                new_name=str(up.get("nombre") or "").strip()[:160],
            )
        )
    for rel in data.get("relaciones") or []:
        if not isinstance(rel, dict):
            continue
        src, dst = _key(rel.get("src")), _key(rel.get("dst"))
        rt = str(rel.get("tipo") or "").strip().lower()
        if not src or not dst or rt not in store.RELATION_TYPES:
            out.dropped.append(f"relacion:invalida:{str(rel)[:60]}")
            continue
        out.relations.append(Relation(src, dst, rt, _conf(rel.get("confianza"))))
    return out


# --- aplicación ------------------------------------------------------------------------------ #


@dataclass
class ApplyStats:
    assigned: int = 0
    none: int = 0
    created: int = 0
    hitos: int = 0
    updates: int = 0
    closed: int = 0
    relations: int = 0
    promoted: int = 0
    low_confidence: int = 0
    routine_rejected: int = (
        0  #: adheridos por llave que el modelo marcó rutina → evidencia rechazada
    )
    priorities: int = 0  #: prioridades fijadas/cambiadas por el modelo
    renamed: int = 0  #: asuntos renombrados por un cambio de nombre anunciado en el mensaje
    twins: int = 0  #: «nuevos» que ya existían en el catálogo → redirigidos (gemelos)
    dropped: list[str] = field(default_factory=list)
    touched: set[int] = field(default_factory=set)


def apply_parsed(
    conn: Connection,
    user_id: int,
    parsed: Parsed,
    msgs: Sequence[MessageView],
    settings: AsuntosSettings,
    *,
    catalog_ids: set[int],
    twins: Mapping[str, int] | None = None,
) -> ApplyStats:
    """Asienta la respuesta de un lote. Los asuntos nuevos nacen `candidato` y se promueven al
    juntar evidencia (o por semilla fuerte). Mensajes no mencionados quedan «vistos sin asunto».

    `twins`: ref (`nuevo:N`) → asunto del catálogo que YA es esa cosa (`gemelos.resolve_twins`):
    no se crea; el nombre propuesto queda como alias y todo lo que el lote le atribuía (evidencia,
    hito, estado, relaciones) va al existente."""
    st = ApplyStats(dropped=list(parsed.dropped))
    twins = twins or {}
    by_id = {m.id: m for m in msgs}
    ref_map: dict[str, int] = {}
    seen: dict[int, tuple[int | None, str]] = {
        m.id: (
            (m.preassigned, m.preassigned_method or "asunto")
            if m.preassigned is not None
            else (None, "llm")
        )
        for m in msgs
    }
    mentioned: set[int] = set()

    def resolve(key: str | None) -> int | None:
        if key is None:
            return None
        if key.startswith("nuevo:"):
            return ref_map.get(key)
        aid = int(key[1:])
        if aid not in catalog_ids:
            return None
        return store.resolve_merged(conn, user_id, aid)

    # 1) nuevos (candidatos hasta juntar evidencia)
    used_refs = {a.asunto for a in parsed.assignments if a.asunto and a.asunto.startswith("nuevo:")}
    for nv in parsed.new:
        if nv.ref not in used_refs:
            st.dropped.append(f"nuevo:sin_mensajes:{nv.name[:40]}")
            continue
        opener = next(
            (a.inbox_id for a in parsed.assignments if a.asunto == nv.ref and a.inbox_id in by_id),
            None,
        )
        twin = twins.get(nv.ref)
        if twin is not None and twin in catalog_ids:
            # Ya existe esa cosa del mundo en el catálogo: no nace un segundo asunto. El nombre
            # propuesto queda como alias (la próxima vez el nombre igual adhiere sin juez).
            aid = store.resolve_merged(conn, user_id, twin)
            store.absorb_name(conn, user_id, aid, nv.name, entities=nv.entities)
            ref_map[nv.ref] = aid
            st.twins += 1
            st.touched.add(aid)
            _log.info(
                "asuntos.assign.twin_redirected",
                user_id=user_id,
                asunto_id=aid,
                proposed=nv.name[:80],
                inbox_id=opener,
            )
            continue
        aid = store.create_asunto(
            conn,
            user_id,
            name=nv.name,
            kind=nv.kind,
            description=nv.description,
            seed=nv.seed,
            entities=nv.entities,
            opened_by_inbox_id=opener,
            status="candidato",
            priority=nv.priority,
            priority_reason=nv.priority_reason,
            priority_source="llm",
            due_on=nv.due_on,
        )
        ref_map[nv.ref] = aid
        st.created += 1
        st.touched.add(aid)

    # 2) asignaciones (evidencia + hito + llaves)
    for a in parsed.assignments:
        m = by_id.get(a.inbox_id)
        if m is None:
            st.dropped.append(f"asignacion:inbox_fuera_del_lote:{a.inbox_id}")
            continue
        mentioned.add(m.id)
        if m.preassigned is not None:
            if a.asunto is None and not a.hito_text:
                # Sin asunto y SIN hito: el modelo dice que el mensaje adherido por llave es RUTINA
                # (recibo normal, newsletter): se retira del asunto (rechazo definitivo) y queda
                # visto sin asunto. Con hito, la adhesión manda y el hito se asienta (abajo).
                if store.reject_evidence_for(conn, user_id, m.preassigned, m.id):
                    st.routine_rejected += 1
                    st.touched.add(m.preassigned)
                seen[m.id] = (None, "llm")
                st.none += 1
                continue
            # La adhesión manda: solo se toma el hito (la evidencia ya existe).
            model_target = resolve(a.asunto) if a.asunto else None
            if model_target is not None and model_target != m.preassigned:
                st.dropped.append(
                    f"asignacion:preasignado_ignorado:{m.id}:A{model_target}->A{m.preassigned}"
                )
            if a.hito_text:
                store.add_evidence(
                    conn,
                    user_id,
                    m.preassigned,
                    m.id,
                    method=m.preassigned_method or "asunto",
                    hito_date=a.hito_date,
                    hito_text=a.hito_text,
                )
                st.hitos += 1
            st.touched.add(m.preassigned)
            continue
        if a.asunto is None:
            st.none += 1
            continue
        target = resolve(a.asunto)
        if target is None:
            st.dropped.append(f"asignacion:asunto_desconocido:{a.asunto}")
            st.none += 1
            continue
        if a.confidence < settings.min_confidence:
            st.low_confidence += 1
            st.none += 1
            continue
        outcome = store.add_evidence(
            conn,
            user_id,
            target,
            m.id,
            method="llm",
            confidence=a.confidence,
            hito_date=a.hito_date,
            hito_text=a.hito_text,
        )
        if outcome is None:
            # Rechazo previo (usuario/curador): es definitivo, el modelo no lo revierte.
            st.dropped.append(f"asignacion:evidencia_rechazada:{m.id}->A{target}")
            st.none += 1
            continue
        store.add_keys(conn, user_id, target, m.claim_keys)
        seen[m.id] = (target, "llm")
        st.assigned += 1
        st.touched.add(target)
        if a.hito_text:
            st.hitos += 1
    for m in msgs:
        if m.id not in mentioned and m.preassigned is None:
            st.none += 1  # el modelo no lo mencionó: queda «visto» sin asunto (se reporta)

    # 3) estados / cierres
    for up in parsed.updates:
        target = resolve(up.asunto)
        if target is None:
            st.dropped.append(f"actualizacion:asunto_desconocido:{up.asunto}")
            continue
        last_inbox = next(
            (
                a.inbox_id
                for a in reversed(parsed.assignments)
                if (
                    by_id[a.inbox_id].preassigned == target
                    if a.inbox_id in by_id and by_id[a.inbox_id].preassigned is not None
                    else resolve(a.asunto) == target
                )
            ),
            None,
        )
        store.record_state(
            conn,
            user_id,
            target,
            up.state_text,
            closed=up.closed,
            source="llm",
            inbox_id=last_inbox,
        )
        st.updates += 1
        st.closed += int(up.closed)
        if up.priority is not None and store.set_priority(
            conn, user_id, target, up.priority, reason=up.priority_reason, source="llm"
        ):
            st.priorities += 1
        if up.due_set:
            store.set_due(conn, user_id, target, up.due_on, source="llm")
        if up.new_name:
            # Cambio de nombre anunciado por el mensaje: el anterior queda como alias. Un asunto
            # silenciado no se toca (el dueño lo cerró a propósito).
            cur = store.get_asunto(conn, user_id, target)
            if (
                cur is not None
                and not cur.silenced
                and cur.name.casefold() != up.new_name.casefold()
            ):
                store.rename(conn, user_id, target, up.new_name)
                st.renamed += 1
                _log.info(
                    "asuntos.renamed_by_message",
                    user_id=user_id,
                    asunto_id=target,
                    old=cur.name[:80],
                    new=up.new_name[:80],
                    inbox_id=last_inbox,
                )
        st.touched.add(target)

    # 4) relaciones
    for rel in parsed.relations:
        s, d = resolve(rel.src), resolve(rel.dst)
        if s is None or d is None:
            st.dropped.append(f"relacion:asunto_desconocido:{rel.src}->{rel.dst}")
            continue
        if rel.confidence < max(settings.min_confidence, _MIN_RELATION_CONFIDENCE):
            st.low_confidence += 1
            continue
        if store.add_relation(
            conn, user_id, s, d, rel.relation_type, confidence=rel.confidence, source="llm"
        ):
            st.relations += 1
        st.touched.update((s, d))

    # 5) estado inicial de los asuntos nuevos que ninguna actualización del lote dejó escrito
    #    (nacen con su primer hito o su descripción; una actualización posterior lo pisa).
    for aid in sorted(set(ref_map.values())):
        store.seed_state(conn, user_id, aid)
    # 6) promoción de candidatos + marca «visto»
    for aid in sorted(st.touched):
        if store.promote_if_ready(conn, user_id, aid, min_evidence_open=settings.min_evidence_open):
            st.promoted += 1
    store.mark_seen(conn, user_id, [(iid, aid, method) for iid, (aid, method) in seen.items()])
    return st


# --- etapa ---------------------------------------------------------------------------------- #


@dataclass
class AssignResult:
    considered: int = 0
    purged: int = 0  #: asuntos vaciados por `force` que nadie volvió a elegir (borrados al final)
    demoted: int = 0  #: abiertos que `force` dejó con poca evidencia → candidato
    closed_by_date: int = 0  #: eventos/hackatones ya pasados (hito, plazo o sin fecha) → cerrados
    due_cleared: int = 0  #: plazos vencidos con señal posterior → borrados (ruido)
    closed_paid: int = 0  #: asuntos de pago con pago posterior registrado en finanzas → cerrados
    inactivated: int = 0  #: abiertos sin señal en `inactive_days` → inactivos
    reactivated: int = 0  #: inactivos con señal nueva → abiertos
    relevant: int = 0
    skipped_seen: int = 0
    adhered: int = 0
    llm_batches: int = 0
    assigned: int = 0
    none: int = 0
    created: int = 0
    promoted: int = 0
    hitos: int = 0
    updates: int = 0
    closed: int = 0
    relations: int = 0
    renamed: int = 0  #: asuntos renombrados porque el mensaje anunció el cambio de nombre
    twins: int = 0  #: «nuevos» del modelo que ya existían en el catálogo → redirigidos
    low_confidence: int = 0
    dropped: int = 0
    rejected: int = 0  #: mensajes que el proveedor rechazó por contenido (quedan «vistos»)
    errors: int = 0
    woven: int = 0
    cost_usd: float = 0.0
    touched: set[int] = field(default_factory=set)
    #: Solo en dry-run: qué habría decidido el modelo por mensaje (asunto/nuevo/null, hito).
    proposals: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("touched", "proposals")}
        d["touched"] = len(self.touched)
        d["proposals"] = len(self.proposals)
        return d


def _relevant(conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> list[int]:
    """Mensajes que entran al asignador: correos con veredicto `relevant` del gate; lo que no es
    correo (chats, notas) no pasa por el gate y entra directo."""
    if not inbox_ids:
        return []
    return [
        int(x)
        for x in conn.execute(
            text(
                "SELECT i.id FROM inbox i JOIN sources s ON s.id = i.source_id "
                "WHERE i.user_id = :u AND i.id = ANY(:ids) AND ("
                "  s.type <> ALL(:email_types) OR EXISTS ("
                "    SELECT 1 FROM relevance_verdicts v "
                "    WHERE v.inbox_id = i.id AND v.verdict = 'relevant'))"
            ),
            {"u": user_id, "ids": list(inbox_ids), "email_types": EMAIL_TYPES},
        ).scalars()
    ]


def _unseen(conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> list[int]:
    if not inbox_ids:
        return []
    seen = {
        int(x)
        for x in conn.execute(
            text(
                "SELECT inbox_id FROM mod_asuntos_seen WHERE user_id = :u AND inbox_id = ANY(:ids)"
            ),
            {"u": user_id, "ids": list(inbox_ids)},
        ).scalars()
    }
    return [i for i in inbox_ids if i not in seen]


#: Errores 4xx que NO son de contenido (credenciales, saldo, permisos, rate limit): se propagan.
_NON_CONTENT_4XX = frozenset({401, 402, 403, 404, 408, 429})


def _is_content_rejection(e: BaseException) -> bool:
    """¿El proveedor rechazó el contenido del lote? (4xx de request, no de cuenta/cuota)."""
    if not isinstance(e, LLMError):
        return False
    return 400 <= e.status_code < 500 and e.status_code not in _NON_CONTENT_4XX


async def _ask(llm: LLMClient, messages: list[ChatMessage]) -> LLMResult:
    return await llm.complete(
        messages, response_format="json_object", temperature=0.0, max_tokens=_MAX_TOKENS
    )


async def run_assign(
    user_id: int,
    inbox_ids: Sequence[int],
    *,
    force: bool = False,
    client: LLMClient | None = None,
    settings: AsuntosSettings | None = None,
    dry_run: bool = False,
) -> AssignResult:
    """Etapa `asuntos` sobre una ventana de mensajes: relevantes y no vistos (o todos si `force`).
    `dry_run`: experimento — juzga con el modelo y devuelve `proposals`, sin escribir nada.

    Commit por lote (una conexión por lote): una falla del LLM deja esos mensajes sin marca
    «visto» y se reintentan en la próxima corrida; los lotes anteriores quedan asentados.
    """
    if dry_run and force:
        raise ValueError("dry_run y force son incompatibles (force borra decisiones previas)")
    res = AssignResult(considered=len(inbox_ids))
    with connection() as conn:
        cfg = settings or get_settings(conn, user_id)
        if not cfg.enabled:
            _log.info("asuntos.assign.disabled", user_id=user_id)
            return res
        relevant = _relevant(conn, user_id, inbox_ids)
        forced_asuntos: list[int] = []
        if force:
            # Reproceso: olvida la decisión previa y las llaves que esos mensajes sostenían; la
            # adhesión se salta en esta corrida (el punto de `force` es RE-DECIDIR con el modelo).
            pre = load_messages(conn, user_id, relevant, body_chars=cfg.body_chars)
            forced_asuntos = store.asuntos_of(conn, user_id, relevant)
            store.forget(conn, user_id, relevant)
            store.delete_keys(conn, user_id, [k for m in pre for k in m.claim_keys])
        res.relevant = len(relevant)
        pending = relevant if force else _unseen(conn, user_id, relevant)
        res.skipped_seen = len(relevant) - len(pending)
        msgs = load_messages(conn, user_id, pending, body_chars=cfg.body_chars)
    if not msgs:
        return res

    # La adhesión corre POR LOTE (antes de cada llamada), no una sola vez al inicio: así los
    # mensajes tardíos de la ventana se pegan a los asuntos que los lotes anteriores acaban de
    # abrir (y sus llaves acaban de registrar) sin gastar LLM.
    adhesion = cfg.adhesion_enabled and not force
    llm = client or build_llm_client(CONSUMER, user_id=user_id)
    owns = client is None
    try:
        batches = [msgs[i : i + cfg.batch_size] for i in range(0, len(msgs), cfg.batch_size)]
        for k, batch in enumerate(batches):
            with bound_log_context(asuntos_batch=k, asuntos_batch_size=len(batch)):
                if adhesion:
                    batch = (
                        _adhere_batch_dry(user_id, batch, res)
                        if dry_run
                        else _adhere_batch(user_id, batch, res)
                    )
                    if not batch:
                        continue
                try:
                    await _run_batch(user_id, batch, cfg, llm, res, dry_run=dry_run)
                except Exception as e:
                    res.errors += 1
                    _log.warning(
                        "asuntos.assign.batch_error",
                        user_id=user_id,
                        batch=k,
                        exc_type=type(e).__name__,
                        exc_msg=str(e)[:300],
                        body=str(getattr(e, "body", "") or "")[:600] or None,
                    )
    finally:
        if owns:
            await aclose_llm(llm)
    if not dry_run:
        with connection() as conn:
            store.raise_priority_for_deadlines(conn, user_id)
            # Reglas deterministas al cierre de la corrida (no hay que esperar al mantenimiento):
            # un evento/hackatón cuyo último hito ya pasó, con plazo vencido o sin ninguna fecha y
            # semanas sin señal se cierra solo; lo abierto sin señal en `inactive_days` pasa a
            # inactivo. Un asunto que ya pasó no debe quedar abierto hasta que alguien corra el
            # curador (pedido del dueño, 2026-09-17).
            res.closed_by_date = store.close_expired(
                conn, user_id, undated_days=cfg.inactive_days // 2
            )
            res.due_cleared = store.clear_stale_due(conn, user_id)
            res.closed_paid = finance_link.close_paid(conn, user_id)
            # La inactividad se mide contra el mensaje más nuevo de la corrida, no contra hoy:
            # un backfill de mensajes viejos no debe inactivar lo que en ese momento estaba vivo.
            newest = conn.execute(
                text("SELECT max(occurred_at) FROM inbox WHERE user_id = :u AND id = ANY(:ids)"),
                {"u": user_id, "ids": list(inbox_ids)},
            ).scalar()
            res.inactivated, res.reactivated = store.sweep_inactivity(
                conn, user_id, inactive_days=cfg.inactive_days, ref=newest
            )
    if force and forced_asuntos:
        # Lo que el force vació: sin evidencia → fuera; abierto con un solo correo → candidato;
        # cerrados/inactivos intactos. Al FINAL, para que durante la corrida sigan en el catálogo.
        with connection() as conn:
            res.purged, res.demoted = store.settle_after_force(
                conn, user_id, forced_asuntos, min_evidence_open=cfg.min_evidence_open
            )
    if not dry_run:
        _weave(user_id, res)
    _log.info("asuntos.assign.done", user_id=user_id, dry_run=dry_run, **res.as_dict())
    return res


def _adhere_batch_dry(
    user_id: int, batch: list[MessageView], res: AssignResult
) -> list[MessageView]:
    """Como `_adhere_batch` pero SIN escribir (dry-run): calcula qué pegaría por llaves y marca
    esos mensajes `preassigned` para que el LLM (y el reporte) los vean como en la corrida real."""
    with connection() as conn:
        glued = adhere(conn, user_id, batch)
    res.adhered += len(glued)
    return [
        replace(m, preassigned=glued[m.id][0], preassigned_method=glued[m.id][1])
        if m.id in glued
        else m
        for m in batch
    ]


def _adhere_batch(user_id: int, batch: list[MessageView], res: AssignResult) -> list[MessageView]:
    """Pega por llaves deterministas lo que ya tiene asunto vivo (evidencia + «visto» al instante)
    y devuelve el lote COMPLETO con esos mensajes marcados `preassigned`: el LLM no decide su
    asunto, pero sí lee su hito/estado."""
    with connection() as conn:
        glued = adhere(conn, user_id, batch)
        for m in batch:
            hit = glued.get(m.id)
            if hit is None:
                continue
            aid, method = hit
            store.add_evidence(conn, user_id, aid, m.id, method=method, confidence=1.0)
            res.touched.add(aid)
        store.mark_seen(conn, user_id, [(i, a, meth) for i, (a, meth) in glued.items()])
    res.adhered += len(glued)
    return [
        replace(m, preassigned=glued[m.id][0], preassigned_method=glued[m.id][1])
        if m.id in glued
        else m
        for m in batch
    ]


async def _run_batch(
    user_id: int,
    batch: list[MessageView],
    cfg: AsuntosSettings,
    llm: LLMClient,
    res: AssignResult,
    *,
    dry_run: bool = False,
) -> None:
    # Catálogo CONTEMPORÁNEO al lote: los cerrados/inactivos entran si su actividad se solapa con
    # las fechas de estos mensajes (± ventana), no con hoy — así el histórico se pega a lo que ya
    # existía en su época en vez de abrir duplicados.
    stamps = [m.occurred_at for m in batch]
    with connection() as conn:
        cat = store.catalog(
            conn,
            user_id,
            closed_window_days=cfg.closed_window_days,
            around=(min(stamps), max(stamps)),
        )
    messages = build_messages(cat, batch)
    started = datetime.now()
    try:
        result = await _ask(llm, messages)
    except Exception as e:
        latency = int((datetime.now() - started) / timedelta(milliseconds=1))
        record_llm_call(
            user_id=user_id,
            purpose=PURPOSE,
            model="?",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=Decimal("0"),
            latency_ms=latency,
            status="error",
            error_message=(str(e) + " " + str(getattr(e, "body", "") or ""))[:500],
            metadata={"inbox_ids": [m.id for m in batch]},
        )
        if _is_content_rejection(e):
            # El proveedor rechazó el CONTENIDO del lote (p. ej. DeepSeek «Content Exists
            # Risk»): bisecar para aislar el mensaje culpable; el resto se decide normal y el
            # culpable queda «visto» sin asunto (reintentarlo cada corrida no arregla nada).
            if len(batch) > 1:
                mid = len(batch) // 2
                _log.info(
                    "asuntos.assign.batch_split",
                    user_id=user_id,
                    size=len(batch),
                    body=str(getattr(e, "body", "") or "")[:200] or None,
                )
                await _run_batch(user_id, batch[:mid], cfg, llm, res, dry_run=dry_run)
                await _run_batch(user_id, batch[mid:], cfg, llm, res, dry_run=dry_run)
                return
            if not dry_run:
                with connection() as conn:
                    store.mark_seen(conn, user_id, [(batch[0].id, None, "rechazado_llm")])
            res.rejected += 1
            _log.warning(
                "asuntos.assign.message_rejected",
                user_id=user_id,
                inbox_id=batch[0].id,
                body=str(getattr(e, "body", "") or "")[:200] or None,
            )
            return
        raise
    parsed = parse_response(result.content)
    if any(d.startswith("json:") for d in parsed.dropped):
        # Respuesta inutilizable: NUNCA marcar «visto» (los mensajes se reintentan). Si fue por
        # truncamiento (razonamiento + salida > max_tokens), bisecar como con los rechazos.
        record_llm_call(
            user_id=user_id,
            purpose=PURPOSE,
            model=result.model,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            cache_hit_tokens=result.usage.cache_hit_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            status="error",
            error_message=(
                f"respuesta inválida ({parsed.dropped[0]}, finish={result.finish_reason})"
            ),
            response_text=result.content[:2000] if result.content else None,
            metadata={"inbox_ids": [m.id for m in batch], "finish_reason": result.finish_reason},
        )
        res.cost_usd += float(result.cost_usd)
        if result.finish_reason == "length" and len(batch) > 1:
            mid = len(batch) // 2
            _log.info(
                "asuntos.assign.batch_truncated",
                user_id=user_id,
                size=len(batch),
                completion_tokens=result.usage.completion_tokens,
            )
            await _run_batch(user_id, batch[:mid], cfg, llm, res, dry_run=dry_run)
            await _run_batch(user_id, batch[mid:], cfg, llm, res, dry_run=dry_run)
            return
        raise RuntimeError(
            f"respuesta del LLM inválida ({parsed.dropped[0]}, finish={result.finish_reason})"
        )
    if dry_run:
        # Experimento: se juzga y se REPORTA (propuestas), no se escribe nada ni se marca visto.
        by_id = {a.id: a for a in cat}
        pre_by_inbox = {m.id: m for m in batch if m.preassigned is not None}
        for a in parsed.assignments:
            target = a.asunto
            label: str | None
            pre = pre_by_inbox.get(a.inbox_id)
            if pre is not None and pre.preassigned is not None:
                ex = by_id.get(pre.preassigned)
                name = ex.name if ex else "?"
                label = f"A{pre.preassigned} {name} [adherido:{pre.preassigned_method}]"
            elif target and target.startswith("A") and target[1:].isdigit():
                ex = by_id.get(int(target[1:]))
                label = f"A{ex.id} {ex.name} [{ex.status}]" if ex else f"{target} (fuera)"
            elif target:
                new = next((n for n in parsed.new if n.ref == target), None)
                label = f"NUEVO {new.name} ({new.kind})" if new else target
            else:
                label = None
            res.proposals.append(
                {
                    "inbox_id": a.inbox_id,
                    "asunto": label,
                    "confianza": a.confidence,
                    "hito": a.hito_text,
                }
            )
        res.llm_batches += 1
        res.cost_usd += float(result.cost_usd)
        record_llm_call(
            user_id=user_id,
            purpose=PURPOSE,
            model=result.model,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            cache_hit_tokens=result.usage.cache_hit_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            status="ok",
            metadata={"inbox_ids": [m.id for m in batch], "dry_run": True},
            response_text=result.content[:4000] if result.content else None,
        )
        return
    # Un «nuevo» que ya es una cosa del catálogo (otro nombre, otro canal) se redirige ANTES de
    # asentar: regla barata + juez atómico (`gemelos`). Principio 2: una cosa, un registro.
    twins = gemelos.twins_as_ids(
        await gemelos.resolve_twins(user_id, gemelos.as_proposed(parsed.new), cat)
    )
    with connection() as conn:
        stats = apply_parsed(
            conn, user_id, parsed, batch, cfg, catalog_ids={a.id for a in cat}, twins=twins
        )
    # Las tareas que recién se colgaron de un asunto (extraídas antes de saber cuál) se comparan
    # ahora con las de ese asunto: la extracción no pudo, porque no había asunto.
    from memex.modules.tareas.extract import recheck_duplicates

    await recheck_duplicates(user_id)
    res.llm_batches += 1
    res.assigned += stats.assigned
    res.none += stats.none
    res.created += stats.created
    res.twins += stats.twins
    res.promoted += stats.promoted
    res.hitos += stats.hitos
    res.updates += stats.updates
    res.closed += stats.closed
    res.relations += stats.relations
    res.renamed += stats.renamed
    res.low_confidence += stats.low_confidence
    res.dropped += len(stats.dropped)
    res.cost_usd += float(result.cost_usd)
    res.touched.update(stats.touched)
    record_llm_call(
        user_id=user_id,
        purpose=PURPOSE,
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cache_hit_tokens=result.usage.cache_hit_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        status="ok",
        response_text=result.content,
        metadata={
            "inbox_ids": [m.id for m in batch],
            "finish_reason": result.finish_reason,
            "reasoning_tokens": getattr(result.usage, "reasoning_tokens", None),
            "catalog": len(cat),
            "assigned": stats.assigned,
            "none": stats.none,
            "created": stats.created,
            "hitos": stats.hitos,
            "updates": stats.updates,
            "relations": stats.relations,
            "renamed": stats.renamed,
            "dropped": stats.dropped[:20],
        },
    )
    if stats.dropped:
        _log.info("asuntos.assign.dropped", user_id=user_id, dropped=stats.dropped[:20])


def _weave(user_id: int, res: AssignResult) -> None:
    """Teje las aristas del grafo de los asuntos tocados (incremental, solo esos)."""
    if not res.touched:
        return
    from memex.relations.deterministic import weave_asuntos

    with connection() as conn:
        res.woven = weave_asuntos(conn, user_id, sorted(res.touched))


async def run_pending(
    user_id: int, *, limit: int = 400, client: LLMClient | None = None, dry_run: bool = False
) -> AssignResult:
    """Job incremental: decide los relevantes aún no vistos, en orden cronológico (tope `limit`
    por corrida para acotar la duración; lo que sobre entra en la siguiente)."""
    with connection() as conn:
        cfg = get_settings(conn, user_id)
        if not cfg.enabled:
            return AssignResult()
        ids = store.unseen_relevant_inbox_ids(conn, user_id, limit=limit)
    if not ids:
        return AssignResult()
    return await run_assign(user_id, ids, client=client, settings=cfg, dry_run=dry_run)


def run_assign_sync(user_id: int, inbox_ids: Sequence[int], *, force: bool = False) -> AssignResult:
    return asyncio.run(run_assign(user_id, inbox_ids, force=force))
