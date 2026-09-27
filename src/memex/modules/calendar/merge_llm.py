"""Merge/enriquecimiento LLM de la consolidación (ADR-015 §4): suma la info extra de las copias.

La consolidación (`consolidate.py`) agrupa duplicados confirmados y elige un ganador por prioridad
de forma DETERMINISTA. Este paso va un poco más allá: cuando un consolidado tiene >1 evento crudo
(copias del mismo evento de fuentes distintas), le pregunta al LLM cómo COMBINAR los campos de
texto (título / lugar / descripción) para que el evento canónico tenga TODA la info — ej. una copia
trae el lugar, otra una nota. La fecha/hora siguen siendo deterministas (del ganador); el LLM solo
enriquece lo descriptivo.

ESTABLE + barato: se guarda una `merge_signature` (hash de la membresía + contenido del grupo) en
`mod_calendar_consolidated`; re-correr NO re-llama al LLM salvo que el grupo cambie. Así no se gasta
de más ni se churnéa la firma del write-back (que dispararía un loop). Best-effort por consolidado.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import CostAccum, record_llm_call
from memex.db import connection
from memex.llm import ChatMessage, LLMClient, LLMResult, aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.modules.calendar.prompt import CALENDAR_MERGE_SYSTEM_PROMPT
from memex.modules.calendar.settings import llm_on_past_events

_log = get_logger("memex.modules.calendar.merge_llm")

_DEFAULT_LIMIT = 200
_MAX_TOKENS = 512


@dataclass(frozen=True)
class MergeMember:
    """Una copia del evento (sus campos de texto) para mostrarle al LLM."""

    title: str
    location: str
    description: str


@dataclass(frozen=True)
class MergedText:
    """El resultado del merge: campos de texto combinados."""

    title: str
    location: str
    description: str


@dataclass
class MergeStats:
    consolidated: int = 0  # consolidados con >1 miembro considerados
    merged: int = 0  # enriquecidos por el LLM (membresía nueva/cambiada)
    skipped: int = 0  # sin cambios desde el último merge (estables)
    errors: int = 0
    #: Costo LLM acumulado. Calendar es source-less; el total bajo `calendar.merge.end`
    #: atribuye el costo a calendar (visible, no perdido).
    cost: CostAccum = field(default_factory=CostAccum)


def _member_signature(winner_first: list[MergeMember]) -> str:
    """Hash estable de la membresía + contenido de texto del grupo (orden ganador-primero)."""
    payload = json.dumps(
        [[m.title, m.location, m.description] for m in winner_first], ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_merged(content: str, fallback: MergeMember) -> MergedText:
    """Parsea la respuesta del LLM. Falla/ambigüedad → cae al texto del ganador (fallback)."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return MergedText(fallback.title, fallback.location, fallback.description)
    if not isinstance(data, dict):
        return MergedText(fallback.title, fallback.location, fallback.description)
    title = data.get("title")
    return MergedText(
        title=str(title) if isinstance(title, str) and title.strip() else fallback.title,
        location=str(data.get("location") or fallback.location),
        description=str(data.get("description") or fallback.description),
    )


async def merge_text(
    llm: LLMClient, winner_first: list[MergeMember]
) -> tuple[MergedText, LLMResult]:
    """Combina los campos de texto de las copias (la primera es la principal). Devuelve el texto
    combinado + el LLMResult (para registrar el costo)."""
    blocks = []
    for i, m in enumerate(winner_first):
        label = "PRINCIPAL" if i == 0 else f"copia {i}"
        loc = m.location or "(sin lugar)"
        desc = m.description or "(sin descripción)"
        blocks.append(f"[{label}] título={m.title!r}, lugar={loc!r}, descripción={desc!r}")
    user_content = "Copias del mismo evento a combinar:\n" + "\n".join(blocks)
    result = await llm.complete(
        [
            ChatMessage("system", CALENDAR_MERGE_SYSTEM_PROMPT),
            ChatMessage("user", user_content),
        ],
        response_format="json_object",
        temperature=0.0,
        max_tokens=_MAX_TOKENS,
    )
    return _parse_merged(result.content, winner_first[0]), result


# --- worker ------------------------------------------------------------------------ #


@dataclass(frozen=True)
class _ConsGroup:
    cons_id: int
    stored_signature: str | None
    members: list[MergeMember]  # ganador primero


#: Un consolidado está VENCIDO si su fecha efectiva de fin quedó antes de hoy.
_CONS_IS_CURRENT_SQL = "COALESCE(ends_on, starts_on) >= CURRENT_DATE"


def _count_past_groups(conn: Connection, user_id: int) -> int:
    """Consolidados VENCIDOS multi-copia que el gate de `llm_on_past_events` deja sin enriquecer
    (para el log: nada de saltos silenciosos)."""
    return int(
        conn.execute(
            text(
                f"""
                SELECT count(*)
                FROM mod_calendar_consolidated c
                WHERE c.user_id = :uid AND NOT c.deleted AND NOT ({_CONS_IS_CURRENT_SQL})
                  AND (SELECT count(*) FROM mod_calendar_event_links l
                        WHERE l.consolidated_id = c.id) > 1
                """
            ),
            {"uid": user_id},
        ).scalar_one()
    )


def _load_groups(
    conn: Connection, user_id: int, limit: int, *, include_past: bool
) -> list[_ConsGroup]:
    """Consolidados NO borrados con >1 miembro, sus copias (ganador primero) y la firma guardada."""
    past_filter = "" if include_past else f"AND {_CONS_IS_CURRENT_SQL}"
    cons_rows = (
        conn.execute(
            text(
                f"""
                SELECT id, winner_event_id, merge_signature
                FROM mod_calendar_consolidated
                WHERE user_id = :uid AND NOT deleted
                  {past_filter}
                ORDER BY id
                LIMIT :limit
                """
            ),
            {"uid": user_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    groups: list[_ConsGroup] = []
    for c in cons_rows:
        cons_id = int(c["id"])
        member_rows = (
            conn.execute(
                text(
                    """
                    SELECT e.id, e.title, e.location, e.description
                    FROM mod_calendar_event_links l
                    JOIN mod_calendar_events e ON e.id = l.event_id
                    WHERE l.consolidated_id = :cid
                    ORDER BY e.id
                    """
                ),
                {"cid": cons_id},
            )
            .mappings()
            .all()
        )
        if len(member_rows) <= 1:
            continue  # un solo crudo → no hay nada que combinar
        winner_id = c["winner_event_id"]
        ordered = sorted(member_rows, key=lambda r: (r["id"] != winner_id, int(r["id"])))
        members = [
            MergeMember(
                title=str(r["title"]),
                location=str(r["location"]),
                description=str(r["description"]),
            )
            for r in ordered
        ]
        groups.append(
            _ConsGroup(
                cons_id=cons_id,
                stored_signature=(str(c["merge_signature"]) if c["merge_signature"] else None),
                members=members,
            )
        )
    return groups


def _apply_merge(conn: Connection, cons_id: int, merged: MergedText, signature: str) -> None:
    conn.execute(
        text(
            """
            UPDATE mod_calendar_consolidated
            SET title = :title, location = :location, description = :description,
                merge_signature = :sig, updated_at = NOW()
            WHERE id = :id
            """
        ),
        {
            "id": cons_id,
            "title": merged.title,
            "location": merged.location,
            "description": merged.description,
            "sig": signature,
        },
    )


async def run_merge(
    user_id: int,
    *,
    limit: int = _DEFAULT_LIMIT,
    client: LLMClient | None = None,
) -> MergeStats:
    """Enriquece con el LLM los consolidados multi-copia cuyo grupo cambió desde el último merge.

    Idempotente vía `merge_signature`. Best-effort por consolidado. `client` inyectable.

    Si `llm_on_past_events` está apagado (default), los consolidados VENCIDOS no se enriquecen
    (sin gasto); se retoman si la perilla se prende."""
    stats = MergeStats()
    with connection() as conn:
        include_past = llm_on_past_events(conn, user_id)
        groups = _load_groups(conn, user_id, limit, include_past=include_past)
        if not include_past:
            skipped_past = _count_past_groups(conn, user_id)
            if skipped_past:
                _log.info("calendar.merge.past_skipped", user_id=user_id, groups=skipped_past)
    if not groups:
        _log.info("calendar.merge.empty", user_id=user_id)
        return stats

    owns_client = client is None
    llm: LLMClient = client or build_llm_client("calendar_merge", user_id=user_id)
    _log.info("calendar.merge.start", user_id=user_id, groups=len(groups))
    try:
        for group in groups:
            stats.consolidated += 1
            signature = _member_signature(group.members)
            if signature == group.stored_signature:
                stats.skipped += 1  # sin cambios desde el último merge → estable, no re-llama
                continue
            try:
                merged, result = await merge_text(llm, group.members)
            except Exception as e:  # best-effort: un grupo fallido no frena los demás
                stats.errors += 1
                _log.error(
                    "calendar.merge.group_failed",
                    cons_id=group.cons_id,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                continue
            with connection() as conn:
                _apply_merge(conn, group.cons_id, merged, signature)
            record_llm_call(
                user_id=user_id,
                purpose="calendar_merge",
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cache_hit_tokens=result.usage.cache_hit_tokens,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                status="ok",
                # El consolidado combina copias de varios sources → sin source; se
                # identifica por purpose="calendar_merge".
                source_id=None,
                metadata={"cons_id": group.cons_id, "members": len(group.members)},
            )
            stats.cost.calls += 1
            stats.cost.prompt_tokens += result.usage.prompt_tokens
            stats.cost.completion_tokens += result.usage.completion_tokens
            stats.cost.cost_usd += result.cost_usd
            stats.merged += 1
    finally:
        if owns_client:
            await aclose_llm(llm)

    _log.info(
        "calendar.merge.end",
        user_id=user_id,
        consolidated=stats.consolidated,
        merged=stats.merged,
        skipped=stats.skipped,
        errors=stats.errors,
        # Costo total de calendar (todo sin source); el evento calendar.* lo atribuye a calendar.
        llm_calls=stats.cost.calls,
        llm_cost_usd=str(stats.cost.cost_usd),
    )
    return stats
