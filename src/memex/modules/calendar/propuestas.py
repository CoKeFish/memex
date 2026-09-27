"""Una PROPUESTA no es un evento (2026-09-26).

El dueño: «si digo "qué tal si hacemos X en una semana" y nadie confirma, no hay nada». En un
chat de grupo, una idea, propuesta o encuesta de fechas («les propongo…», «¿quién se apunta?»,
«para confirmar fechas», «podríamos…») no fija nada: no es compromiso ni anuncio. Solo hay
evento cuando el hilo lo deja como hecho (fecha y hora fijadas por quien organiza, «confirmado»,
«queda para el 26») o cuando la propia persona dice que va.

El prompt del extractor lo dice; esta guarda determinista lo garantiza aunque el modelo se
tiente: un evento que sale SOLO de mensajes de chat, con estado `anuncio` o `provisional`, cuyo
texto de origen (o su evidencia) suena a propuesta, se descarta antes de insertarse. Lo
`confirmado` (la persona dijo que va) nunca se toca; los correos tampoco (un boletín que
«propone» una fecha suele anunciarla de verdad)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.modules.calendar.commitment import CONFIRMADO
from memex.modules.calendar.schema import CalendarEventItem

_log = get_logger("memex.modules.calendar.propuestas")

#: Marcas de propuesta, idea o encuesta de fechas (español informal de chat). Generales: no
#: nombran eventos ni personas.
PROPOSAL_MARKERS = (
    r"\bles?\s+propongo\b",
    r"\bpropongo\b",
    r"\bproponemos\b",
    r"\bqu[eé]\s+tal\s+si\b",
    r"\bqui[eé]n(?:es)?\s+se\s+(?:apunta|anima|suma)n?\b",
    r"\bse\s+apuntan\b",
    r"\bpara\s+confirmar\s+(?:la\s+)?fecha",
    r"\ba\s+ver\s+qui[eé]n\b",
    r"\bpodr[ií]amos\b",
    r"\bdeber[ií]amos\s+hacer\b",
    r"\bles\s+parece\b",
    r"\bqu[eé]\s+opinan\b",
    r"\bvotemos\b",
    r"\bencuesta\b",
    r"\bsi\s+les\s+sirve\b",
    r"\bqu[eé]\s+d[ií]a\s+les\s+(?:queda|sirve|conviene)\b",
)
_PROPOSAL_RX = re.compile("|".join(PROPOSAL_MARKERS), re.IGNORECASE)
#: Marcas de que el hilo YA fijó el plan: pesan más que la propuesta.
FIXED_MARKERS = (
    r"\bconfirmad[oa]s?\b",
    r"\bqued[aoó]\s+(?:para|el)\b",
    r"\bquedamos\s+(?:para|el|en)\b",
    r"\bfecha\s+(?:fijada|definida|cerrada)\b",
)
_FIXED_RX = re.compile("|".join(FIXED_MARKERS), re.IGNORECASE)


def sounds_like_proposal(text_: str) -> bool:
    """¿El texto propone o pregunta en vez de anunciar un hecho? Una marca de plan fijado
    («confirmado», «queda para el 26») gana sobre la propuesta."""
    t = " ".join(text_.split())
    if not t:
        return False
    if _FIXED_RX.search(t):
        return False
    return _PROPOSAL_RX.search(t) is not None


def _chat_texts(conn: Connection, inbox_ids: Sequence[int]) -> dict[int, str] | None:
    """id → texto para mensajes de CHAT; None si alguna fuente no es un chat (correo, documento):
    la guarda solo aplica a eventos que salen enteramente de chats."""
    ids = [int(i) for i in inbox_ids]
    if not ids:
        return None
    rows = conn.execute(
        text("SELECT id, payload FROM inbox WHERE id = ANY(:ids)"), {"ids": ids}
    ).all()
    if len(rows) != len(ids):
        return None
    out: dict[int, str] = {}
    for iid, payload in rows:
        p: dict[str, Any] = payload or {}
        if "chat_id" not in p:
            return None
        out[int(iid)] = str(p.get("text") or p.get("media_caption") or "")
    return out


def drop_chat_proposals(
    conn: Connection, user_id: int, events: Sequence[CalendarEventItem]
) -> tuple[list[CalendarEventItem], int]:
    """Filtra los eventos que son solo una propuesta de chat sin confirmar. Devuelve (los que
    quedan, cuántos se descartaron)."""
    kept: list[CalendarEventItem] = []
    dropped = 0
    for ev in events:
        if ev.commitment == CONFIRMADO:
            kept.append(ev)
            continue
        texts = _chat_texts(conn, ev.source_inbox_ids)
        if texts is None:
            kept.append(ev)
            continue
        sample = " ".join([ev.evidence, ev.commitment_evidence, *texts.values()])
        if sounds_like_proposal(sample):
            dropped += 1
            _log.info(
                "calendar.skip.proposal",
                user_id=user_id,
                title=ev.title[:80],
                starts_on=ev.starts_on.isoformat(),
                commitment=ev.commitment,
                inbox_ids=list(ev.source_inbox_ids),
            )
            continue
        kept.append(ev)
    return kept, dropped
