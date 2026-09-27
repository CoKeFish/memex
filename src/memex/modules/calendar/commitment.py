"""ESTADO de un evento: ¿qué tan atada está la persona a que ocurra? Vocabulario de calendario
(RFC 5545 `STATUS`, Google Calendar, Outlook), no inventado:

- `confirmado` (iCal CONFIRMED): está inscrita, pagó, reservó o confirmó (el correo habla de SU
  cupo, SU cita, SU matrícula).
- `provisional` (iCal TENTATIVE): le hablan como participante concreta pero no consta que
  aceptara ni pagara.
- `anuncio`: convocatoria, evento abierto, curso, newsletter, plazo general; no es un compromiso
  de la persona, pero se registra para tener la oportunidad en el mapa.

La columna se llama `commitment` (histórico, interno); en pantalla y en el CLI es «estado».

Quién lo fija (`commitment_source`): el EXTRACTOR al leer el mensaje (`extraction`); el JUEZ codex
(`judge`, `memex-calendar-sync classify`) para lo que se extrajo antes de que existiera el campo o
para una segunda opinión; una REGLA determinista (`rule`) que solo SUBE provisional → confirmado
cuando la persona le pagó a la organización remitente (±60 días); `manual`/`provider` para lo que
la persona puso a mano o trae de su propio calendario; `user` cuando lo cambia por el CLI.

Diseño validado por experimento (2026-09-09, 18 eventos): el texto acierta 18/18; las señales
deterministas solas dan falsos confirmados porque son señales de la ORGANIZACIÓN, no del evento.
Por eso el texto manda y lo determinista solo corrobora, nunca sube un anuncio.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.llm import ChatMessage, LLMClient
from memex.llm.codex import CodexClient
from memex.logging import get_logger
from memex.modules.principios import PRINCIPIOS_GENERALES

_log = get_logger("memex.modules.calendar.commitment")

CONFIRMADO = "confirmado"
PROVISIONAL = "provisional"
ANUNCIO = "anuncio"
LEVELS: tuple[str, ...] = (CONFIRMADO, PROVISIONAL, ANUNCIO)
RANK: dict[str, int] = {CONFIRMADO: 2, PROVISIONAL: 1, ANUNCIO: 0}

#: Normalización de lo que devuelve un LLM (o escribe una persona): variantes en español/inglés
#: y los nombres viejos del campo (firme/probable/informativo) → nivel canónico.
_ALIASES: dict[str, str] = {
    "confirmado": CONFIRMADO,
    "confirmada": CONFIRMADO,
    "confirmed": CONFIRMADO,
    "aceptado": CONFIRMADO,
    "accepted": CONFIRMADO,
    "firme": CONFIRMADO,
    "firm": CONFIRMADO,
    "inscrito": CONFIRMADO,
    "inscrita": CONFIRMADO,
    "pagado": CONFIRMADO,
    "seguro": CONFIRMADO,
    "provisional": PROVISIONAL,
    "tentativo": PROVISIONAL,
    "tentativa": PROVISIONAL,
    "tentative": PROVISIONAL,
    "probable": PROVISIONAL,
    "likely": PROVISIONAL,
    "posible": PROVISIONAL,
    "invitado": PROVISIONAL,
    "invitada": PROVISIONAL,
    "quizas": PROVISIONAL,
    "quizás": PROVISIONAL,
    "maybe": PROVISIONAL,
    "anuncio": ANUNCIO,
    "announcement": ANUNCIO,
    "informativo": ANUNCIO,
    "informational": ANUNCIO,
    "convocatoria": ANUNCIO,
    "abierto": ANUNCIO,
    "open": ANUNCIO,
    "info": ANUNCIO,
}

#: Etiqueta humana (CLI, bot, dashboard): la misma palabra que el valor.
LABEL: dict[str, str] = {
    CONFIRMADO: "confirmado",
    PROVISIONAL: "provisional",
    ANUNCIO: "anuncio",
}

#: Qué significa cada estado, en una frase (ayudas del CLI, tooltips).
DESCRIPTION: dict[str, str] = {
    CONFIRMADO: "estás inscrito, pagaste o confirmaste",
    PROVISIONAL: "te hablan como participante, sin confirmación",
    ANUNCIO: "convocatoria o evento abierto; no es un compromiso tuyo",
}


def normalize_level(value: object, default: str = ANUNCIO) -> str:
    """Cualquier variante → estado canónico; desconocido/vacío → `default` (no se descarta nada)."""
    key = str(value or "").strip().lower()
    return _ALIASES.get(key, default)


def max_level(levels: list[str] | tuple[str, ...]) -> str:
    """El estado más alto de un grupo (una vez confirmado, el grupo está confirmado)."""
    best = ANUNCIO
    for lv in levels:
        if RANK.get(lv, 0) > RANK[best]:
            best = lv
    return best


JUDGE_SYSTEM_PROMPT = (
    "Sos un juez que lee un mensaje y decide el ESTADO de un evento que se extrajo de ese mensaje "
    "para la persona dueña del buzón, con el vocabulario de un calendario. Tres estados:\n"
    "- confirmado: la persona está inscrita, pagó, reservó o confirmó; el mensaje le habla de SU "
    "cupo, SU cita, SU matrícula, SU vuelo, SU turno, SU clase.\n"
    "- provisional: le hablan como participante o invitada concreta (la citan, la esperan, le "
    "piden confirmar) pero no consta que haya aceptado ni pagado.\n"
    "- anuncio: convocatoria, evento o curso ABIERTO, newsletter, plazo general; cualquiera "
    "podría recibirlo.\n\n" + PRINCIPIOS_GENERALES + "\n"
    "Por los principios 1 y 3: el estado se juzga por la situación de la PROPIA persona. En un\n"
    "chat de grupo, lo que otro miembro cuenta de sí mismo («estuve», «me inscribí», «voy») no\n"
    "la compromete: a lo sumo 'anuncio'. Solo sube a provisional o confirmado lo que le dicen a\n"
    "ella con nombre o lo que ella misma escribe (remitente «yo (la propia persona)»).\n"
    'Respondé SOLO JSON: {"level": "confirmado|provisional|anuncio", "confidence": 0.0-1.0, '
    '"evidence": "<frase textual corta del mensaje que lo justifica>"}'
)


@dataclass(frozen=True)
class Verdict:
    level: str
    confidence: float
    evidence: str


def parse_verdict(content: str) -> Verdict:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return Verdict(ANUNCIO, 0.0, "")
    if not isinstance(data, dict):
        return Verdict(ANUNCIO, 0.0, "")
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    return Verdict(
        normalize_level(data.get("level")),
        max(0.0, min(1.0, conf)),
        str(data.get("evidence", ""))[:300],
    )


def judge_prompt(event: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    parts = [
        f"EVENTO extraído: título={event['title']!r}, fecha={event['starts_on']}, "
        f"hora={event.get('start_time') or 'sin hora'}, "
        f"lugar={event.get('location') or '(sin lugar)'}.",
        "",
    ]
    for m in messages[:2]:
        body = " ".join(str(m.get("body") or "").split())[:3500]
        parts.append(
            f"MENSAJE #{m['id']} · de: {m.get('sender') or ''} · asunto: {m.get('subject') or ''}\n"
            f"{body}\n"
        )
    return "\n".join(parts)


async def judge_event(
    client: LLMClient, event: dict[str, Any], messages: list[dict[str, Any]]
) -> Verdict:
    res = await client.complete(
        [
            ChatMessage(role="system", content=JUDGE_SYSTEM_PROMPT),
            ChatMessage(role="user", content=judge_prompt(event, messages)),
        ],
        response_format="json_object",
    )
    return parse_verdict(res.content)


# --- DB ------------------------------------------------------------------------------- #

_PENDING_SQL = """
SELECT e.id, e.title, e.starts_on, e.start_time, e.location, e.source_inbox_ids
FROM mod_calendar_events e
WHERE e.user_id = :uid AND e.origin = 'extraction' AND e.commitment_source IS NULL
  AND e.provider_status IS DISTINCT FROM 'cancelled'
  AND (COALESCE(e.ends_on, e.starts_on) >= :since OR cardinality(e.recurrence) > 0)
ORDER BY e.starts_on, e.id
LIMIT :limit
"""

_MESSAGES_SQL = """
SELECT id, COALESCE(payload->>'subject','') AS subject, COALESCE(payload->>'from','') AS sender,
       COALESCE(payload->>'body_text','') AS body
FROM inbox WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id
"""

#: Corroboración determinista: la persona le PAGÓ (±60 días) a la organización que manda el
#: mensaje (contraparte resuelta a identidad, o a su organización padre). Solo sube
#: provisional → confirmado.
_PAID_ORG_SQL = """
SELECT count(*) FROM mod_finance_consolidated f
WHERE f.user_id = :uid AND NOT f.deleted AND f.status = 'completada' AND f.direction = 'egreso'
  AND f.counterparty_identity_id IN (
      SELECT m.resolved_identity_id FROM mod_identidades_mentions m
      WHERE m.user_id = :uid AND m.source_inbox_ids && CAST(:ids AS bigint[])
        AND m.resolved_identity_id IS NOT NULL
      UNION
      SELECT i.parent_identity_id FROM mod_identidades_mentions m
      JOIN mod_identidades i ON i.id = m.resolved_identity_id
      WHERE m.user_id = :uid AND m.source_inbox_ids && CAST(:ids AS bigint[])
        AND i.parent_identity_id IS NOT NULL)
  AND f.occurred_at >= NOW() - INTERVAL '60 days'
"""


def set_commitment(
    conn: Connection,
    user_id: int,
    event_id: int,
    level: str,
    *,
    source: str,
    evidence: str = "",
) -> None:
    conn.execute(
        text(
            "UPDATE mod_calendar_events SET commitment = :c, commitment_source = :s, "
            "commitment_evidence = :e WHERE user_id = :u AND id = :i"
        ),
        {
            "c": normalize_level(level),
            "s": source,
            "e": evidence[:300],
            "u": user_id,
            "i": event_id,
        },
    )


def paid_sender_org(conn: Connection, user_id: int, inbox_ids: list[int]) -> bool:
    if not inbox_ids:
        return False
    return (
        int(conn.execute(text(_PAID_ORG_SQL), {"uid": user_id, "ids": inbox_ids}).scalar() or 0) > 0
    )


def apply_upgrade_rules(conn: Connection, user_id: int) -> int:
    """Regla determinista que solo SUBE: provisional + pago a la organización remitente →
    confirmado. Nunca toca un anuncio (las señales de organización no prueban el evento) ni baja
    nada. Idempotente. Devuelve cuántos subió."""
    rows = conn.execute(
        text(
            "SELECT id, source_inbox_ids FROM mod_calendar_events "
            "WHERE user_id = :uid AND commitment = 'provisional' "
            "AND commitment_source IN ('extraction', 'judge') "
            "AND provider_status IS DISTINCT FROM 'cancelled'"
        ),
        {"uid": user_id},
    ).all()
    n = 0
    for event_id, ids in rows:
        if paid_sender_org(conn, user_id, [int(x) for x in (ids or [])]):
            set_commitment(
                conn,
                user_id,
                int(event_id),
                CONFIRMADO,
                source="rule",
                evidence="pago a la organización remitente en los últimos 60 días",
            )
            n += 1
    if n:
        _log.info("calendar.commitment.upgraded", user_id=user_id, upgraded=n)
    return n


@dataclass
class ClassifyStats:
    judged: int = 0
    confirmado: int = 0
    provisional: int = 0
    anuncio: int = 0
    errors: int = 0


async def run_classify(
    user_id: int,
    *,
    since_days: int = 30,
    limit: int = 200,
    client: LLMClient | None = None,
) -> ClassifyStats:
    """Juzga con codex los eventos EXTRAÍDOS sin estado (`commitment_source IS NULL`) desde
    `since_days` atrás (y todos los maestros recurrentes), leyendo su mensaje fuente. Reparación
    para lo extraído antes del campo y segunda opinión bajo demanda. Best-effort por evento."""
    from datetime import date, timedelta

    stats = ClassifyStats()
    since = date.today() - timedelta(days=since_days)
    with connection() as conn:
        events = [
            dict(r)
            for r in conn.execute(
                text(_PENDING_SQL), {"uid": user_id, "since": since, "limit": limit}
            ).mappings()
        ]
    if not events:
        return stats
    llm = client or CodexClient()
    for ev in events:
        ids = [int(x) for x in (ev["source_inbox_ids"] or [])]
        with connection() as conn:
            messages = [
                dict(m) for m in conn.execute(text(_MESSAGES_SQL), {"ids": ids}).mappings().all()
            ]
        try:
            verdict = await judge_event(llm, ev, messages)
        except Exception as e:  # best-effort por evento
            stats.errors += 1
            _log.warning("calendar.commitment.judge_failed", event_id=ev["id"], error=str(e)[:200])
            continue
        with connection() as conn:
            set_commitment(
                conn,
                user_id,
                int(ev["id"]),
                verdict.level,
                source="judge",
                evidence=verdict.evidence,
            )
        stats.judged += 1
        setattr(stats, verdict.level, getattr(stats, verdict.level) + 1)
        _log.info(
            "calendar.commitment.judged",
            event_id=ev["id"],
            level=verdict.level,
            confidence=verdict.confidence,
        )
    return stats
