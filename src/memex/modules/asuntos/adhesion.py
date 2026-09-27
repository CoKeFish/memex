"""Vista de mensaje para asuntos + ADHESIÓN determinista (sin LLM).

Un mensaje nuevo se pega a un asunto VIVO (candidato/abierto) cuando comparte con su evidencia:
- `hilo`: su In-Reply-To/References apunta a un Message-ID ya visto en el asunto (o al revés);
- `evento`: nombra el mismo evento de Luma/Calendar («Registration confirmed for X», «⏰ X is
  starting», «Invitación: X …», «Update for X»);
- `asunto`: mismo asunto normalizado (sin RE/FWD, emojis, números ni sesiones «S01») desde el
  mismo dominio remitente.
La línea base del experimento mostró que estas llaves agrupan con pureza 100 % (pero cobertura
baja): por eso ADHIEREN a lo que ya existe y nunca abren asuntos. Las llaves se registran cuando un
mensaje entra a un asunto (por adhesión o por el asignador), así el índice crece solo. El mensaje
adherido igual va al LLM, marcado como ya asignado, para que aporte hito/estado. El mensaje
adherido igual va al LLM, marcado como ya asignado, para que aporte hito/estado.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.html_text import strip_invisible_padding
from memex.modules.asuntos import store
from memex.modules.chat_context import ChatModes, chat_modes_for_source

#: URL suelta o entre «<>» (así las deja el texto plano de un boletín: «texto <https://…>»).
_URL = re.compile(r"<?\bhttps?://[^\s<>\"']+>?", re.I)
#: Una URL más larga que esto es de rastreo (beehiiv, safelinks, utm) y no dice nada: queda solo
#: el host. Las cortas (un Meet, un Luma, un repositorio) sí pueden ser el dato que importa.
_MAX_URL_CHARS = 80

_QUOTE_CUT = re.compile(
    r"(\n\s*(De|From|Enviado el|Sent|Para|To):\s.{0,200}\n)"
    r"|(-{3,}\s*(Original|Mensaje original|Forwarded))"
    r"|(\bEl .{5,90} escribi[oó]:)|(\bOn .{5,90} wrote:)",
    re.I,
)
_WS = re.compile(r"\s+")
_PREFIX = re.compile(r"^\s*((re|fwd?|rv|fw|aw|wg)\s*:\s*)+", re.I)
_NOISE = re.compile(r"[^\w\s]|\d+|\bs\d+\b|_", re.U)
_EVENT_PATTERNS = (
    re.compile(
        r"^(?:registration (?:confirmed|approved|pending approval)|you.re registered|"
        r"you are invited to|update for|new message in|reminder)\s*:?\s*(?:for\s+)?(.+)$",
        re.I,
    ),
    re.compile(r"^[⏰🔔]?\s*(.+?)\s+(?:is starting|starts|comienza|empieza)\b", re.I),
    re.compile(
        r"^(?:invitaci[oó]n(?: actualizada)?|invitation(?: updated)?|recordatorio|reminder)"
        r"\s*:\s*(.+)$",
        re.I,
    ),
)
_EVENT_TAIL = re.compile(
    r"\s+(?:lun|mar|mi[eé]|jue|vie|s[aá]b|dom|mon|tue|wed|thu|fri|sat|sun)\b.*$|\s+@\s+.*$|\s*\[.*$",
    re.I,
)
_GENERIC_SUBJECTS = frozenset(
    {
        "invitacion",
        "recordatorio",
        "actualizacion",
        "update",
        "reminder",
        "hola",
        "hello",
        "varios",
        "informacion",
        "importante",
        "urgente",
        "notificacion",
    }
)
_MIN_SUBJECT_KEY = 12
_MIN_EVENT_KEY = 6


@dataclass(frozen=True)
class MessageView:
    id: int
    source_id: int
    kind: str  # email | chat | social | ?
    occurred_at: datetime
    subject: str
    sender_addr: str
    sender_name: str
    body: str
    message_id: str | None
    thread_refs: tuple[str, ...]  # In-Reply-To + References (Message-IDs a los que responde)
    is_list: bool
    is_reply: bool
    lookup_keys: tuple[tuple[str, str], ...] = field(default=())
    claim_keys: tuple[tuple[str, str], ...] = field(default=())
    #: Adherido por llave determinista ANTES del LLM: el modelo no decide su asunto (ya está), pero
    #: sí lee el mensaje para hitos y estado (una respuesta en el hilo del grado es justo donde el
    #: estado cambia; saltarla por ahorrar la llamada dejaba el estado viejo).
    preassigned: int | None = None
    preassigned_method: str | None = None
    #: Solo chats: título del grupo, naturaleza (`anuncios|programa|intimo`, de la allowlist de la
    #: fuente) y si el mensaje lo escribió el propio usuario (perspectiva).
    chat_title: str = ""
    chat_mode: str = ""
    is_self: bool = False
    #: Solo chats: «quién: «texto»» del mensaje al que responde (principio 3: el asignador lee el
    #: hilo, no la frase suelta). Vacío si no responde a nada o no se encontró el original.
    reply_quote: str = ""

    @property
    def sender_domain(self) -> str:
        return self.sender_addr.rsplit("@", 1)[-1].lower() if "@" in self.sender_addr else ""

    @property
    def sender_label(self) -> str:
        if self.sender_name and self.sender_addr:
            return f"{self.sender_name} <{self.sender_addr}>"
        return self.sender_addr or self.sender_name


def _shorten_url(m: re.Match[str]) -> str:
    url = m.group(0).strip("<>")
    if len(url) <= _MAX_URL_CHARS:
        return url
    host = url.split("://", 1)[1].split("/", 1)[0]
    return f"{host}/…"


def clean_body(raw: str, n: int) -> str:
    """Cuerpo que ve el asignador: sin relleno invisible, sin URLs de rastreo, sin citas y
    recortado a `n` caracteres. La limpieza va ANTES del recorte: un boletín de beehiiv traía 200
    caracteres invisibles de preheader más un link de rastreo de 400, y con el recorte primero el
    modelo nunca llegó a leer «X pasó a ser Y» (2026-09-15: abrió un asunto duplicado)."""
    b = _URL.sub(_shorten_url, strip_invisible_padding(raw or ""))
    m = _QUOTE_CUT.search(b)
    if m and m.start() > 200:
        b = b[: m.start()]
    return _WS.sub(" ", b).strip()[:n]


def normalize_subject(s: str) -> str:
    s = _PREFIX.sub("", s or "")
    s = _NOISE.sub(" ", s.lower())
    return _WS.sub(" ", s).strip()


def event_key(subject: str) -> str | None:
    s = (subject or "").strip()
    for rx in _EVENT_PATTERNS:
        m = rx.match(s)
        if m:
            name = _EVENT_TAIL.sub("", m.group(1)).strip()
            key = normalize_subject(name)
            return key[:80] if len(key) >= _MIN_EVENT_KEY else None
    return None


def _ids(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    if isinstance(raw, list):
        vals = [str(x) for x in raw]
    else:
        vals = re.findall(r"<[^<>\s]+>", str(raw)) or [str(raw)]
    return tuple(v.strip().strip("<>").lower() for v in vals if v.strip())


def _sender(f: Any) -> tuple[str, str]:
    if isinstance(f, dict):
        return (
            str(f.get("address") or f.get("email") or "").lower(),
            str(f.get("name") or ""),
        )
    if isinstance(f, list) and f:
        return _sender(f[0])
    if isinstance(f, str):
        m = re.match(r"\s*(?:\"?([^\"<]*)\"?\s*)?<?([^<>\s]+@[^<>\s]+)>?", f)
        if m:
            return m.group(2).lower(), (m.group(1) or "").strip()
        return f.lower(), ""
    return "", ""


def compute_keys(m: MessageView) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """(llaves para BUSCAR un asunto vivo, llaves que RECLAMA al entrar a uno)."""
    lookup: list[tuple[str, str]] = [("hilo", r) for r in m.thread_refs]
    claim: list[tuple[str, str]] = list(lookup)
    if m.message_id:
        claim.append(("hilo", m.message_id))
    ev = event_key(m.subject)
    if ev:
        lookup.append(("evento", ev))
        claim.append(("evento", ev))
    ns = normalize_subject(m.subject)
    if len(ns) >= _MIN_SUBJECT_KEY and ns not in _GENERIC_SUBJECTS and m.sender_domain:
        k = f"{m.sender_domain}|{ns}"[:300]
        lookup.append(("asunto", k))
        claim.append(("asunto", k))
    return tuple(dict.fromkeys(lookup)), tuple(dict.fromkeys(claim))


#: Cuánto del mensaje citado ve el asignador (mismo tope que el extractor).
_REPLY_QUOTE_CHARS = 160


def _quote_of(payload: dict[str, Any]) -> str:
    """«quién: «texto»» de un mensaje de chat, para citarlo como el original de una respuesta."""
    snd = payload.get("sender")
    who = "alguien"
    if isinstance(snd, dict):
        if snd.get("is_self"):
            who = "el usuario"
        else:
            who = str(snd.get("display_name") or snd.get("username") or "") or "alguien"
    quoted = " ".join(str(payload.get("text") or payload.get("media_caption") or "").split())
    return f"{who}: «{quoted[:_REPLY_QUOTE_CHARS]}»"


def reply_quotes(conn: Connection, user_id: int, rows: Sequence[Any]) -> dict[int, str]:
    """inbox_id → cita del mensaje al que responde, para los chats del lote. El original se busca
    primero en el lote y, si no está (respondió a algo de otra ventana), en la tabla."""
    by_msg: dict[tuple[int, str, str], dict[str, Any]] = {}
    for r in rows:
        p = r["payload"] or {}
        if "chat_id" in p and p.get("message_id") is not None:
            by_msg[(int(r["source_id"]), str(p.get("chat_id")), str(p.get("message_id")))] = p
    out: dict[int, str] = {}
    for r in rows:
        p = r["payload"] or {}
        target = p.get("reply_to_message_id")
        if not target or "chat_id" not in p:
            continue
        key = (int(r["source_id"]), str(p.get("chat_id")), str(target))
        ref = by_msg.get(key)
        if ref is None:
            ref = conn.execute(
                text(
                    "SELECT payload FROM inbox WHERE user_id = :u AND source_id = :s "
                    "AND payload->>'chat_id' = :c AND payload->>'message_id' = :m LIMIT 1"
                ),
                {"u": user_id, "s": key[0], "c": key[1], "m": key[2]},
            ).scalar()
            if isinstance(ref, dict):
                by_msg[key] = ref
        if isinstance(ref, dict):
            out[int(r["id"])] = _quote_of(ref)
    return out


def load_messages(
    conn: Connection, user_id: int, inbox_ids: Sequence[int], *, body_chars: int
) -> list[MessageView]:
    """Mensajes en orden cronológico con cuerpo limpio (sin citas) y llaves calculadas."""
    if not inbox_ids:
        return []
    rows = (
        conn.execute(
            text(
                """
                SELECT i.id, i.source_id, i.occurred_at, i.payload, s.type AS source_type
                FROM inbox i JOIN sources s ON s.id = i.source_id
                WHERE i.user_id = :u AND i.id = ANY(:ids)
                ORDER BY i.occurred_at, i.id
                """
            ),
            {"u": user_id, "ids": list(inbox_ids)},
        )
        .mappings()
        .all()
    )
    quotes = reply_quotes(conn, user_id, rows)
    out: list[MessageView] = []
    modes_by_source: dict[int, ChatModes] = {}
    for r in rows:
        p: dict[str, Any] = r["payload"] or {}
        addr, name = _sender(p.get("from") or p.get("sender") or p.get("sender_name"))
        subject = str(p.get("subject") or "")
        body = str(p.get("body_text") or p.get("text") or p.get("body") or "")
        if not subject and body:
            subject = body[:80]
        try:
            from memex.sources import kind_for_type

            kind = kind_for_type(str(r["source_type"])).value
        except Exception:
            kind = "?"
        thread = tuple(dict.fromkeys(_ids(p.get("in_reply_to")) + _ids(p.get("references"))))
        chat_title = chat_mode = ""
        is_self = False
        if kind == "chat":
            sid = int(r["source_id"])
            if sid not in modes_by_source:
                modes_by_source[sid] = chat_modes_for_source(conn, sid)
            chat_title = str(p.get("chat_title") or "")[:80]
            chat_mode = modes_by_source[sid].of(p)
            snd = p.get("sender")
            is_self = bool(snd.get("is_self")) if isinstance(snd, dict) else False
        mv = MessageView(
            id=int(r["id"]),
            source_id=int(r["source_id"]),
            kind=kind,
            occurred_at=r["occurred_at"],
            subject=subject[:200],
            sender_addr=addr,
            sender_name=name[:80],
            body=clean_body(body, body_chars),
            message_id=(_ids(p.get("message_id")) or (None,))[0],
            thread_refs=thread,
            is_list=bool(p.get("list_id")),
            is_reply=bool(p.get("in_reply_to")),
            chat_title=chat_title,
            chat_mode=chat_mode,
            is_self=is_self,
            reply_quote=quotes.get(int(r["id"]), "") if kind == "chat" else "",
        )
        lookup, claim = compute_keys(mv)
        out.append(MessageView(**{**mv.__dict__, "lookup_keys": lookup, "claim_keys": claim}))
    return out


def adhere(
    conn: Connection, user_id: int, msgs: Iterable[MessageView]
) -> dict[int, tuple[int, str]]:
    """Mensaje → (asunto vivo, método) por llaves deterministas. Prioridad hilo > evento > asunto.
    Registra las llaves del mensaje en el asunto al pegarlo (el índice crece con cada adhesión)."""
    out: dict[int, tuple[int, str]] = {}
    for m in msgs:
        if not m.lookup_keys:
            continue
        found = store.find_by_keys(conn, user_id, m.lookup_keys)
        if not found:
            continue
        chosen: tuple[int, str] | None = None
        for kind in ("hilo", "evento", "asunto"):
            hits = [aid for (k, _), aid in found.items() if k == kind]
            if hits:
                chosen = (store.resolve_merged(conn, user_id, hits[0]), kind)
                break
        if chosen is None or store.is_rejected(conn, user_id, chosen[0], m.id):
            continue  # un rechazo previo (usuario/curador) es definitivo para la adhesión
        out[m.id] = chosen
        store.add_keys(conn, user_id, chosen[0], m.claim_keys)
    return out
