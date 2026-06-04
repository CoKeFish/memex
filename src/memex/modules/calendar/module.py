"""`CalendarModule` — extractor de FECHAS/EVENTOS + dominio consolidado (ADR-015 §4, §11).

Es el segundo módulo (después de finance) y ejercita lo que finance no tocó:
- capacidad `provide_domain`: es single-writer de `mod_calendar_events` (el handle de lectura
  vive en `memex.modules.calendar.domain`);
- dedup determinista FASE 1 dentro de `persist`: tras insertar los eventos del lote, compara
  (nuevos entre sí + nuevos contra existentes del user en la ventana de fechas) con la pura
  `mark_duplicates` y registra los pares candidatos en `mod_calendar_dedup_candidates`. Todo en
  `ctx.conn` (la tx que abre el orquestador) → eventos + pares + cursor son atómicos. NUNCA
  fusiona ni borra: los eventos coexisten. La FASE 2 (LLM por par ambiguo) se difiere.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.source import HealthResult, SourceKind
from memex.logging import get_logger
from memex.modules.calendar.dedup import DedupRow, mark_duplicates
from memex.modules.calendar.prompt import CALENDAR_SYSTEM_PROMPT
from memex.modules.calendar.schema import CalendarEventItem
from memex.modules.contract import CAP_EXTRACT, CAP_PROVIDE_DOMAIN, ExtractionItem, ModuleContext

_log = get_logger("memex.modules.calendar")

#: Margen de fechas alrededor del lote para traer existentes comparables en el dedup.
_DEDUP_DATE_MARGIN = timedelta(days=1)


def _insert_events(
    conn: Connection, user_id: int, events: Sequence[CalendarEventItem]
) -> list[DedupRow]:
    """Inserta los eventos y devuelve un `DedupRow` por cada uno (con su `id` recién asignado)."""
    rows: list[DedupRow] = []
    for e in events:
        event_id = conn.execute(
            text(
                """
                INSERT INTO mod_calendar_events
                  (user_id, source_inbox_ids, title, starts_on, ends_on,
                   start_time, end_time, location, description, evidence)
                VALUES
                  (:uid, :ids, :title, :starts_on, :ends_on,
                   :start_time, :end_time, :location, :description, :evidence)
                RETURNING id
                """
            ),
            {
                "uid": user_id,
                "ids": list(e.source_inbox_ids),
                "title": e.title,
                "starts_on": e.starts_on,
                "ends_on": e.ends_on,
                "start_time": e.start_time,
                "end_time": e.end_time,
                "location": e.location,
                "description": e.description,
                "evidence": e.evidence,
            },
        ).scalar_one()
        rows.append(
            DedupRow(
                event_id=int(event_id),
                title=e.title,
                location=e.location,
                starts_on=e.starts_on,
                ends_on=e.ends_on,
                start_time=e.start_time,
                end_time=e.end_time,
            )
        )
    return rows


def _existing_rows(conn: Connection, user_id: int, new_rows: list[DedupRow]) -> list[DedupRow]:
    """Eventos ya persistidos del user comparables con el lote (ventana de fechas ± margen),
    excluyendo los recién insertados."""
    new_ids = [r.event_id for r in new_rows]
    starts = [r.starts_on for r in new_rows]
    lo = min(starts) - _DEDUP_DATE_MARGIN
    hi = max(starts) + _DEDUP_DATE_MARGIN
    rows = (
        conn.execute(
            text(
                """
                SELECT id, title, location, starts_on, ends_on, start_time, end_time
                FROM mod_calendar_events
                WHERE user_id = :uid
                  AND (starts_on BETWEEN :lo AND :hi
                       OR (ends_on IS NOT NULL AND ends_on BETWEEN :lo AND :hi))
                  AND NOT (id = ANY(:new_ids))
                """
            ),
            {"uid": user_id, "lo": lo, "hi": hi, "new_ids": new_ids},
        )
        .mappings()
        .all()
    )
    return [
        DedupRow(
            event_id=int(r["id"]),
            title=str(r["title"]),
            location=str(r["location"]),
            starts_on=r["starts_on"],
            ends_on=r["ends_on"],
            start_time=r["start_time"],
            end_time=r["end_time"],
        )
        for r in rows
    ]


def _mark_processed(conn: Connection, new_event_ids: list[int], in_pair: set[int]) -> None:
    """Tras el dedup FASE 1, marca `processed_at` + `processing_outcome` de los eventos tocados.

    Los que quedaron en un par candidato (nuevos o existentes) → `'pending'` (esperan la FASE 2
    LLM del slice 2 que confirma/rechaza). Los nuevos SIN par → `'unique'`. Aplica por igual a
    eventos de extracción LLM y de proveedor (requisito: marcar el estado de CADA evento)."""
    pending = sorted(in_pair)
    unique = [i for i in new_event_ids if i not in in_pair]
    if pending:
        conn.execute(
            text(
                "UPDATE mod_calendar_events SET processed_at = NOW(), "
                "processing_outcome = 'pending' WHERE id = ANY(:ids)"
            ),
            {"ids": pending},
        )
    if unique:
        conn.execute(
            text(
                "UPDATE mod_calendar_events SET processed_at = NOW(), "
                "processing_outcome = 'unique' WHERE id = ANY(:ids)"
            ),
            {"ids": unique},
        )


def _mark_dedup(conn: Connection, user_id: int, new_rows: list[DedupRow]) -> int:
    """Corre el dedup determinista FASE 1, registra los pares candidatos y marca el estado de
    procesamiento de los eventos nuevos. Devuelve cuántos pares marcó."""
    existing = _existing_rows(conn, user_id, new_rows)
    pairs = mark_duplicates(new_rows, existing)
    in_pair: set[int] = set()
    for p in pairs:
        in_pair.update((p.a_id, p.b_id))
    if pairs:
        conn.execute(
            text(
                """
                INSERT INTO mod_calendar_dedup_candidates
                  (user_id, event_a_id, event_b_id, reason, score)
                VALUES (:uid, :a, :b, :reason, :score)
                ON CONFLICT (event_a_id, event_b_id) DO NOTHING
                """
            ),
            [
                {"uid": user_id, "a": p.a_id, "b": p.b_id, "reason": p.reason, "score": p.score}
                for p in pairs
            ],
        )
    _mark_processed(conn, [r.event_id for r in new_rows], in_pair)
    if pairs:
        _log.info("calendar.dedup.marked", pairs=len(pairs))
    return len(pairs)


class CalendarModule:
    """Extrae eventos a `mod_calendar_events` y marca duplicados candidatos (sin fusionar)."""

    slug: ClassVar[str] = "calendar"
    interest: ClassVar[str] = (
        "Fechas y eventos de la persona: citas, reuniones, clases, exámenes, entregas, vuelos, "
        "turnos médicos, vencimientos, cumpleaños, viajes — cualquier cosa con una fecha "
        "concreta. NO publicidad ni fechas de promociones."
    )
    extraction_schema: ClassVar[type[ExtractionItem]] = CalendarEventItem
    extraction_prompt: ClassVar[str] = CALENDAR_SYSTEM_PROMPT
    capabilities: ClassVar[frozenset[str]] = frozenset({CAP_EXTRACT, CAP_PROVIDE_DOMAIN})
    consumes_kinds: ClassVar[frozenset[SourceKind]] = frozenset({SourceKind.EMAIL, SourceKind.CHAT})
    depends_on: ClassVar[tuple[str, ...]] = ()
    #: `()` = dedup por MECANISMO PROPIO: la unicidad del vértice-evento la da la CONSOLIDACIÓN
    #: (`mod_calendar_consolidated`, ADR-018), no un UNIQUE sobre la fila cruda — los crudos
    #: coexisten; la FASE 1 solo marca pares candidatos.
    identity_fields: ClassVar[tuple[str, ...]] = ()

    async def persist(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Entrypoint del orquestador; delega la unicidad a `self.dedup`."""
        return await self.dedup(ctx, items)

    async def dedup(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Mecanismo propio (`()` en `identity_fields`): inserta los eventos validados y marca pares
        candidatos de duplicado (FASE 1), todo en `ctx.conn` (atómico con el cursor de extracción).
        La unicidad del vértice la da la CONSOLIDACIÓN, no un UNIQUE sobre la fila cruda. Devuelve
        cuántos eventos insertó."""
        events = [i for i in items if isinstance(i, CalendarEventItem)]
        if not events:
            return 0
        new_rows = _insert_events(ctx.conn, ctx.user_id, events)
        _mark_dedup(ctx.conn, ctx.user_id, new_rows)
        return len(events)

    async def health_check(self) -> HealthResult:
        return HealthResult(
            status="healthy", detail="calendar module ready", checked_at=datetime.now(UTC)
        )

    def read_for_inbox(
        self, conn: Connection, user_id: int, inbox_ids: Sequence[int]
    ) -> list[dict[str, Any]]:
        """Eventos públicos (fila cruda) atribuidos a `inbox_ids`. NO expone el estado interno de
        dedup (`mod_calendar_dedup_candidates`, columnas de control de la FASE 1)."""
        rows = (
            conn.execute(
                text(
                    """
                    SELECT title, starts_on, ends_on, start_time, end_time, location, evidence
                    FROM mod_calendar_events
                    WHERE user_id = :uid AND CAST(:ids AS BIGINT[]) && source_inbox_ids
                    ORDER BY id
                    """
                ),
                {"uid": user_id, "ids": list(inbox_ids)},
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]

    def forget_inbox(self, conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> int:
        """Borra los eventos (fila cruda) atribuidos a `inbox_ids` (re-extracción en limpio). NO
        toca el consolidado ni los candidatos de dedup (estado que trasciende al mensaje)."""
        result = conn.execute(
            text(
                """
                DELETE FROM mod_calendar_events
                WHERE user_id = :uid AND CAST(:ids AS BIGINT[]) && source_inbox_ids
                """
            ),
            {"uid": user_id, "ids": list(inbox_ids)},
        )
        return result.rowcount
