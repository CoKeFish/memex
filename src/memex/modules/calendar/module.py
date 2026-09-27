"""`CalendarModule` — extractor de FECHAS/EVENTOS + dominio consolidado (ADR-015 §4, §11).

Es el segundo módulo (después de finance) y ejercita lo que finance no tocó:
- capacidad `provide_domain`: es single-writer de `mod_calendar_events` (el handle de lectura
  vive en `memex.modules.calendar.domain`);
- dedup determinista FASE 1 dentro de `persist`: tras insertar los eventos del lote, compara
  (nuevos entre sí + nuevos contra existentes del user en la ventana de fechas) con la pura
  `mark_duplicates` y registra los pares candidatos en `mod_calendar_dedup_candidates`. Todo en
  `ctx.conn` (la tx que abre el orquestador) → eventos + pares + cursor son atómicos. NUNCA
  fusiona ni borra: los eventos coexisten. La FASE 2 (LLM por par ambiguo) se difiere.
- segunda verificación (`CAP_VERIFY`, `verify.py`): en la fase de extracción, un evento cuya
  fecha queda a ≥ 9 meses de la recepción del mensaje va a un juez atómico (keep/fix/discard);
  una corrección viaja en `item.verification` y `persist` la cuelga de la entidad en la traza.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.localtime import parse_tz, to_local_wallclock
from memex.core.source import HealthResult, SourceKind
from memex.logging import get_logger
from memex.modules.calendar.dedup import DedupPair, DedupRow, mark_duplicates
from memex.modules.calendar.domain import CalendarDomainReader
from memex.modules.calendar.prompt import CALENDAR_SYSTEM_PROMPT
from memex.modules.calendar.propuestas import drop_chat_proposals
from memex.modules.calendar.recurrence import SERIES_PREFIX
from memex.modules.calendar.schema import CalendarEventItem
from memex.modules.calendar.verify import verify_dates
from memex.modules.contract import (
    CAP_EXTRACT,
    CAP_PROVIDE_DOMAIN,
    CAP_VERIFY,
    ExtractionItem,
    ModuleContext,
    VerifyContext,
    VerifyOutcome,
)
from memex.modules.dedup import forget_inbox_rows

if TYPE_CHECKING:
    from memex.core.trace import TraceNode

_log = get_logger("memex.modules.calendar")

#: Margen de fechas alrededor del lote para traer existentes comparables en el dedup.
_DEDUP_DATE_MARGIN = timedelta(days=1)


def _to_local(e: CalendarEventItem) -> CalendarEventItem:
    """Si el evento trae zona EXPLÍCITA (`tz`) y hora, convierte fecha+hora a la hora LOCAL del
    usuario (lo guardado es naive-local). Sin `tz` o sin hora, queda igual. `ends_on` sin
    `end_time` no se toca (un evento multi-día no cambia de días por la zona)."""
    zone = parse_tz(e.tz)
    if zone is None or e.start_time is None:
        return e
    starts_on, start_time = to_local_wallclock(e.starts_on, e.start_time, zone)
    upd: dict[str, object] = {"starts_on": starts_on, "start_time": start_time, "tz": None}
    if e.end_time is not None:
        end_day = e.ends_on or e.starts_on
        ends_on, end_time = to_local_wallclock(end_day, e.end_time, zone)
        upd["end_time"] = end_time
        upd["ends_on"] = ends_on if ends_on != starts_on else None
    return e.model_copy(update=upd)


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
                   start_time, end_time, location, description, evidence, recurrence,
                   recurring_event_id, commitment, commitment_evidence, commitment_source)
                VALUES
                  (:uid, :ids, :title, :starts_on, :ends_on,
                   :start_time, :end_time, :location, :description, :evidence,
                   :recurrence, :series, :commitment, :commitment_evidence, 'extraction')
                RETURNING id
                """
            ),
            {
                "uid": user_id,
                "recurrence": list(e.recurrence_lines()),
                "series": f"{SERIES_PREFIX}{uuid.uuid4()}" if e.recurrence_lines() else None,
                "commitment": e.commitment,
                "commitment_evidence": e.commitment_evidence[:300],
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
                recurrence=e.recurrence_lines(),
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
                SELECT id, title, location, starts_on, ends_on, start_time, end_time, recurrence
                FROM mod_calendar_events
                WHERE user_id = :uid
                  AND (starts_on BETWEEN :lo AND :hi
                       OR (ends_on IS NOT NULL AND ends_on BETWEEN :lo AND :hi)
                       OR cardinality(recurrence) > 0)
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
            recurrence=tuple(r["recurrence"] or ()),
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


def _mark_dedup(conn: Connection, user_id: int, new_rows: list[DedupRow]) -> list[DedupPair]:
    """Corre el dedup determinista FASE 1, registra los pares candidatos y marca el estado de
    procesamiento de los eventos nuevos. Devuelve los pares marcados (para la traza)."""
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
    return pairs


class CalendarModule:
    """Extrae eventos a `mod_calendar_events` y marca duplicados candidatos (sin fusionar)."""

    slug: ClassVar[str] = "calendar"
    #: Lo lee el ruteo Y el prompt agrupado («Interés: …»): si acá solo dijera «eventos de la
    #: persona», el agrupado descartaría los anuncios aunque el prompt del módulo los pida
    #: (medido 2026-09-05: el hackathon del 14 salía con el prompt solo y no en el agrupado).
    interest: ClassVar[str] = (
        "Fechas y eventos con fecha concreta: los compromisos de la persona (citas, reuniones, "
        "clases, exámenes, entregas, vuelos, turnos médicos, vencimientos, cumpleaños, viajes) y "
        "también las convocatorias o eventos abiertos que le anuncian (hackathones, conferencias, "
        "charlas, plazos de inscripción, lanzamientos con fecha), con estado anuncio. "
        "NO ofertas "
        "comerciales ni fechas de publicación de artículos."
    )
    extraction_schema: ClassVar[type[ExtractionItem]] = CalendarEventItem
    extraction_prompt: ClassVar[str] = CALENDAR_SYSTEM_PROMPT
    capabilities: ClassVar[frozenset[str]] = frozenset(
        {CAP_EXTRACT, CAP_PROVIDE_DOMAIN, CAP_VERIFY}
    )
    consumes_kinds: ClassVar[frozenset[SourceKind]] = frozenset(
        {SourceKind.EMAIL, SourceKind.CHAT, SourceKind.NOTES}
    )
    depends_on: ClassVar[tuple[str, ...]] = ()
    optional_deps: ClassVar[tuple[str, ...]] = ()
    #: `()` = dedup por MECANISMO PROPIO: la unicidad del vértice-evento la da la CONSOLIDACIÓN
    #: (`mod_calendar_consolidated`, ADR-018), no un UNIQUE sobre la fila cruda — los crudos
    #: coexisten; la FASE 1 solo marca pares candidatos.
    identity_fields: ClassVar[tuple[str, ...]] = ()

    async def verify(self, items: Sequence[ExtractionItem], ctx: VerifyContext) -> VerifyOutcome:
        """Segunda verificación (fase de extracción, sin DB): fechas a ≥ 9 meses de la recepción
        del mensaje → juez atómico. Ver `verify.py`."""
        return await verify_dates(items, ctx)

    async def persist(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Entrypoint del orquestador; delega la unicidad a `self.dedup`."""
        return await self.dedup(ctx, items)

    async def dedup(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Mecanismo propio (`()` en `identity_fields`): inserta los eventos validados y marca pares
        candidatos de duplicado (FASE 1), todo en `ctx.conn` (atómico con el cursor de extracción).
        La unicidad del vértice la da la CONSOLIDACIÓN, no un UNIQUE sobre la fila cruda. Devuelve
        cuántos eventos insertó."""
        events = [_to_local(i) for i in items if isinstance(i, CalendarEventItem)]
        # Una propuesta de chat sin confirmar («¿qué tal si…?», «¿quién se apunta?») no es
        # evento, ni anuncio: se descarta antes de insertar (`calendar.propuestas`).
        events, _ = drop_chat_proposals(ctx.conn, ctx.user_id, events)
        if not events:
            return 0
        new_rows = _insert_events(ctx.conn, ctx.user_id, events)
        # Traza: una ENTIDAD por evento (no-op si la traza está apagada).
        ents: dict[int, TraceNode] = {}
        for ev, r in zip(events, new_rows, strict=True):
            label = r.title.strip() or "(sin título)"
            if r.starts_on is not None:
                label = f"{label} · {r.starts_on}"
            ents[r.event_id] = ctx.trace.entity(
                "mod_calendar_events",
                id=r.event_id,
                label=label,
                status="ok",
                source_inbox_ids=ev.source_inbox_ids,
            )
            if ev.verification is not None:
                v = ev.verification
                ents[r.event_id].decision(
                    f"{v.field} verificado: {v.original} → {v.corrected}",
                    detail={
                        "original": v.original,
                        "corrected": v.corrected,
                        "rationale": v.rationale,
                        "confidence": v.confidence,
                    },
                    status="warn",
                )
        # Traza: dedup FASE 1 → comparación "vs evento #other" bajo la entidad que toca el par.
        pairs = _mark_dedup(ctx.conn, ctx.user_id, new_rows)
        steps: dict[int, TraceNode] = {}
        for p in pairs:
            for eid in (p.a_id, p.b_id):
                if eid not in ents:
                    continue  # el otro lado es un evento pre-existente, no de este mensaje
                other = p.b_id if eid == p.a_id else p.a_id
                step = steps.get(eid)
                if step is None:
                    step = ents[eid].step("dedup")
                    steps[eid] = step
                step.decision(
                    f"vs evento #{other}",
                    ref=("mod_calendar_events", other),
                    detail={"reason": p.reason, "score": p.score},
                    status="warn",
                )
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
        """Olvida lo aportado por `inbox_ids` a los eventos (fila cruda): les saca la referencia y
        borra solo la fila huérfana. NO toca el consolidado ni los candidatos de dedup (estado que
        trasciende al mensaje)."""
        return forget_inbox_rows(conn, "mod_calendar_events", user_id=user_id, inbox_ids=inbox_ids)

    def provide_domain(self, conn: Connection, user_id: int) -> CalendarDomainReader:
        """Capacidad `provide_domain`: handle del calendario CONSOLIDADO (lectura por rango +
        `contribute` para que otros módulos propongan eventos). Lo reciben (vía
        `ctx.deps['calendar']`) los módulos que declaran `'calendar'` en `depends_on`."""
        return CalendarDomainReader(conn, user_id)


def recheck_candidates(conn: Connection, user_id: int) -> dict[str, int]:
    """Vuelve a correr la FASE 1 sobre TODOS los eventos del user (todos contra todos, no solo
    nuevos contra existentes) con la compuerta VIGENTE, y registra los pares candidatos que
    faltaban. Herramienta de mantenimiento para cuando la compuerta se recalibra (2026-09-08:
    los «Inicio de clases» del Britano nunca fueron par): los pares ya decididos por la FASE 2
    (`confirmed`/`rejected`) no se tocan ni se vuelven a preguntar (`ON CONFLICT DO NOTHING`).
    Devuelve `{"events", "pairs", "new_candidates"}`."""
    rows = [
        DedupRow(
            event_id=int(r["id"]),
            title=str(r["title"]),
            location=str(r["location"]),
            starts_on=r["starts_on"],
            ends_on=r["ends_on"],
            start_time=r["start_time"],
            end_time=r["end_time"],
            recurrence=tuple(r["recurrence"] or ()),
        )
        for r in conn.execute(
            text(
                "SELECT id, title, location, starts_on, ends_on, start_time, end_time, recurrence "
                "FROM mod_calendar_events WHERE user_id = :uid ORDER BY starts_on, id"
            ),
            {"uid": user_id},
        ).mappings()
    ]
    pairs = mark_duplicates(rows, [])
    inserted: list[int] = []
    for p in pairs:
        row = conn.execute(
            text(
                """
                INSERT INTO mod_calendar_dedup_candidates
                  (user_id, event_a_id, event_b_id, reason, score)
                VALUES (:uid, :a, :b, :reason, :score)
                ON CONFLICT (event_a_id, event_b_id) DO NOTHING
                RETURNING event_a_id, event_b_id
                """
            ),
            {"uid": user_id, "a": p.a_id, "b": p.b_id, "reason": p.reason, "score": p.score},
        ).first()
        if row is not None:
            inserted.extend((int(row[0]), int(row[1])))
    if inserted:
        conn.execute(
            text(
                "UPDATE mod_calendar_events SET processed_at = NOW(), "
                "processing_outcome = 'pending' WHERE id = ANY(:ids)"
            ),
            {"ids": sorted(set(inserted))},
        )
    stats = {"events": len(rows), "pairs": len(pairs), "new_candidates": len(inserted) // 2}
    _log.info("calendar.dedup.rechecked", user_id=user_id, **stats)
    return stats
