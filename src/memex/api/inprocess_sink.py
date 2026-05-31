"""Sinks in-process para correr un ingestor DENTRO del proceso API.

`run_ingestor` (en `memex.ingestors.runner`) maneja una `Source` a través del Protocol
`MemexSink` (`memex.core.sink`). El CLI le pasa el `MemexServerClient` (HTTP); el endpoint de
fetch del dashboard, en cambio, corre el ingestor colocado con la DB y no quiere loopear por
HTTP. `sink.py` ya contempla este caso ("an in-process call when ingestor and API are
colocated").

Estos sinks viven en el paquete `api` (NO en `ingestors/`) a propósito: la disciplina de
`tests/test_typing_discipline.py` prohíbe que cualquier módulo bajo `ingestors/` importe
`memex.db` / `memex.api` / `memex.core.inbox` / `memex.core.checkpoint` (ADR-001), justo lo que
un sink DB-backed necesita.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, bindparam, text

from memex.api.ingest_service import ingest_records, resolve_source_type, to_source_record
from memex.api.schemas import IngestRequest
from memex.core import checkpoint, filters
from memex.db import connection


def _sources_by_type(conn: Connection, user_id: int, source_type: str) -> list[dict[str, Any]]:
    rows = (
        conn.execute(
            text(
                """
                SELECT id, user_id, name, type, enabled, config, created_at
                FROM sources
                WHERE user_id = :uid AND type = :t AND enabled = TRUE
                ORDER BY id
                """
            ),
            {"uid": user_id, "t": source_type},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


class InProcessSink:
    """`MemexSink` que persiste contra la DB directamente (sin HTTP).

    Cada método abre su propia `connection()` (commit por llamada) → misma semántica
    transaccional que el camino HTTP, preservando la idempotencia-on-failure del runner:
    si una llamada falla, el checkpoint queda en la última posición flusheada y la próxima
    corrida re-fetchea (dedup por UNIQUE(source_id, external_id)).
    """

    def __init__(self, user_id: int, *, persist_checkpoint: bool = True) -> None:
        self._user_id = user_id
        # range/last son backfills ad-hoc: insertan pero NO deben mover el cursor incremental
        # (si no, una corrida incremental posterior se saltearía el hueco). Con persist=False
        # el insert ocurre igual y el dedup por UNIQUE(source_id, external_id) protege.
        self._persist_checkpoint = persist_checkpoint

    def get_sources_by_type(self, source_type: str) -> list[dict[str, Any]]:
        with connection() as conn:
            return _sources_by_type(conn, self._user_id, source_type)

    def get_checkpoint(self, source_id: int) -> dict[str, Any] | None:
        with connection() as conn:
            return checkpoint.get_cursor(conn, source_id)

    def put_checkpoint(self, source_id: int, cursor: dict[str, Any]) -> None:
        if not self._persist_checkpoint:
            return
        with connection() as conn:
            checkpoint.save_cursor(conn, source_id, cursor)

    def post_ingest_batch(self, records: list[dict[str, Any]]) -> dict[str, int]:
        reqs = [IngestRequest.model_validate(r) for r in records]
        with connection() as conn:
            return ingest_records(conn, self._user_id, reqs)


class DryRunSink:
    """`MemexSink` que CUENTA sin escribir, para el preview del dashboard.

    `post_ingest_batch` aplica los mismos filtros y chequea existencia por external_id, pero
    no inserta nada. `put_checkpoint` es no-op: un dry-run nunca avanza el cursor persistido,
    así la corrida real posterior re-escanea exactamente la misma ventana. `get_checkpoint`
    SÍ devuelve el cursor real para escanear desde donde quedaría la corrida de verdad.
    """

    def __init__(self, user_id: int) -> None:
        self._user_id = user_id

    def get_sources_by_type(self, source_type: str) -> list[dict[str, Any]]:
        with connection() as conn:
            return _sources_by_type(conn, self._user_id, source_type)

    def get_checkpoint(self, source_id: int) -> dict[str, Any] | None:
        with connection() as conn:
            return checkpoint.get_cursor(conn, source_id)

    def put_checkpoint(self, source_id: int, cursor: dict[str, Any]) -> None:
        return  # no-op: el dry-run no debe avanzar el cursor persistido

    def post_ingest_batch(self, records: list[dict[str, Any]]) -> dict[str, int]:
        reqs = [IngestRequest.model_validate(r) for r in records]
        inserted = duplicates = filtered = 0
        by_source: dict[int, list[IngestRequest]] = {}
        for req in reqs:
            by_source.setdefault(req.source_id, []).append(req)

        with connection() as conn:
            for source_id, source_reqs in by_source.items():
                source_type = resolve_source_type(conn, source_id)
                rules = filters.load_active_rules(
                    conn, user_id=self._user_id, source_type=source_type, source_id=source_id
                )
                kept, drops = filters.apply(
                    [to_source_record(r) for r in source_reqs],
                    rules,
                    source_id=source_id,
                    source_type=source_type,
                )
                filtered += sum(drops.values())
                if not kept:
                    continue
                eids = [r.external_id for r in kept]
                stmt = text(
                    "SELECT external_id FROM inbox WHERE source_id = :sid AND external_id IN :eids"
                ).bindparams(bindparam("eids", expanding=True))
                existing = set(conn.execute(stmt, {"sid": source_id, "eids": eids}).scalars().all())
                for r in kept:
                    if r.external_id in existing:
                        duplicates += 1
                    else:
                        inserted += 1
        return {
            "inserted": inserted,
            "duplicates": duplicates,
            "errors": 0,
            "filtered": filtered,
        }
