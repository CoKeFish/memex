"""GET /inbox/coverage — rangos de fechas de origen (occurred_at) ya ingeridos, por fuente.

Cubre: fusión de días con tolerancia (`gap_days`), borde de TZ (un instante UTC que cae en otro
día de pared en Bogotá), fuentes sin items (lane vacía), filtros `kind`/`source_id`, aislamiento
multi-tenant y validación de parámetros (tz inválida / gap negativo → 422).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.core.inbox import insert_record
from memex.core.source import SourceRecord
from memex.db import connection


def _seed_at(source_id: int, user_id: int, whens: list[datetime], prefix: str = "r") -> None:
    with connection() as c:
        for i, occ in enumerate(whens):
            insert_record(
                c,
                user_id=user_id,
                source_id=source_id,
                record=SourceRecord(
                    external_id=f"{prefix}{i}",
                    occurred_at=occ,
                    payload={"i": i},
                    dedupe_keys=[],
                ),
            )


def _mk_source(user_id: int, name: str, type_: str) -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (:u, :n, :t) RETURNING id"),
            {"u": user_id, "n": name, "t": type_},
        ).scalar()
    assert isinstance(sid, int)
    return sid


def _lane(body: dict[str, Any], source_id: int) -> dict[str, Any]:
    matches: list[dict[str, Any]] = [ln for ln in body["lanes"] if ln["id"] == source_id]
    assert len(matches) == 1, f"lane de source {source_id} ausente o duplicada: {body['lanes']}"
    return matches[0]


def test_coverage_merges_with_tolerance(client: Any, seed_source: dict[str, Any]) -> None:
    # Días 1, 2, 3 y 6 de junio (12:00Z): hueco de 2 días faltantes (4 y 5) entre el 3 y el 6.
    _seed_at(
        seed_source["id"],
        1,
        [datetime(2026, 6, d, 12, 0, tzinfo=UTC) for d in (1, 2, 3, 6)],
    )

    r = client.get("/inbox/coverage?tz=UTC&gap_days=2")
    assert r.status_code == 200
    lane = _lane(r.json(), seed_source["id"])
    assert lane["ranges"] == [{"start": "2026-06-01", "end": "2026-06-06", "days": 6, "count": 4}]
    assert lane["total"] == 4
    assert lane["first_day"] == "2026-06-01"
    assert lane["last_day"] == "2026-06-06"

    r = client.get("/inbox/coverage?tz=UTC&gap_days=0")
    lane = _lane(r.json(), seed_source["id"])
    assert lane["ranges"] == [
        {"start": "2026-06-01", "end": "2026-06-03", "days": 3, "count": 3},
        {"start": "2026-06-06", "end": "2026-06-06", "days": 1, "count": 1},
    ]


def test_coverage_tz_boundary(client: Any, seed_source: dict[str, Any]) -> None:
    # 2026-06-02 04:30Z == 2026-06-01 23:30 en Bogotá (UTC-5): el día depende de la tz pedida.
    _seed_at(seed_source["id"], 1, [datetime(2026, 6, 2, 4, 30, tzinfo=UTC)])

    bogota = _lane(client.get("/inbox/coverage?tz=America/Bogota").json(), seed_source["id"])
    assert bogota["ranges"] == [{"start": "2026-06-01", "end": "2026-06-01", "days": 1, "count": 1}]

    utc = _lane(client.get("/inbox/coverage?tz=UTC").json(), seed_source["id"])
    assert utc["ranges"] == [{"start": "2026-06-02", "end": "2026-06-02", "days": 1, "count": 1}]


def test_coverage_empty_inbox(client: Any, seed_source: dict[str, Any]) -> None:
    r = client.get("/inbox/coverage")
    assert r.status_code == 200
    body = r.json()
    lane = _lane(body, seed_source["id"])
    assert lane["ranges"] == []
    assert lane["total"] == 0
    assert lane["first_day"] is None
    assert lane["last_day"] is None
    assert body["domain_min"] is None
    assert body["domain_max"] is None
    # Defaults espejados en la respuesta (el front los muestra).
    assert body["tz"] == "America/Bogota"
    assert body["gap_days"] == 2


def test_coverage_kind_filter(client: Any, seed_source: dict[str, Any]) -> None:
    chat_id = _mk_source(1, "tg-test", "telegram")
    _seed_at(seed_source["id"], 1, [datetime(2026, 6, 1, 12, 0, tzinfo=UTC)], prefix="a")
    _seed_at(chat_id, 1, [datetime(2026, 6, 2, 12, 0, tzinfo=UTC)], prefix="b")

    body = client.get("/inbox/coverage?kind=email").json()
    assert [ln["id"] for ln in body["lanes"]] == [seed_source["id"]]
    assert body["lanes"][0]["kind"] == "email"

    body = client.get("/inbox/coverage?kind=chat").json()
    assert [ln["id"] for ln in body["lanes"]] == [chat_id]
    # El dominio se calcula sobre las lanes filtradas, no sobre todo el inbox.
    assert body["domain_min"] == "2026-06-02"


def test_coverage_unregistered_type_is_other(client: Any) -> None:
    sid = _mk_source(1, "raro", "s")  # tipo sin SourceKind registrada
    lane = _lane(client.get("/inbox/coverage").json(), sid)
    assert lane["kind"] == "other"


def test_coverage_source_id_filter(client: Any, seed_source: dict[str, Any]) -> None:
    other = _mk_source(1, "otra", "imap")
    _seed_at(seed_source["id"], 1, [datetime(2026, 6, 1, 12, 0, tzinfo=UTC)], prefix="a")
    _seed_at(other, 1, [datetime(2026, 6, 2, 12, 0, tzinfo=UTC)], prefix="b")

    body = client.get(f"/inbox/coverage?source_id={other}").json()
    assert [ln["id"] for ln in body["lanes"]] == [other]
    assert body["domain_min"] == "2026-06-02"
    assert body["domain_max"] == "2026-06-02"


def test_coverage_cross_tenant(client: Any, seed_source: dict[str, Any], seed_user2: int) -> None:
    src2 = _mk_source(seed_user2, "ajena", "imap")
    _seed_at(src2, seed_user2, [datetime(2026, 6, 1, 12, 0, tzinfo=UTC)], prefix="u2-")

    body = client.get("/inbox/coverage").json()
    assert [ln["id"] for ln in body["lanes"]] == [seed_source["id"]]
    assert body["domain_min"] is None  # los items del otro usuario no aportan dominio


def test_coverage_domain_spans_lanes(client: Any, seed_source: dict[str, Any]) -> None:
    other = _mk_source(1, "otra", "telegram")
    _seed_at(seed_source["id"], 1, [datetime(2026, 3, 10, 12, 0, tzinfo=UTC)], prefix="a")
    _seed_at(other, 1, [datetime(2026, 6, 2, 12, 0, tzinfo=UTC)], prefix="b")

    body = client.get("/inbox/coverage?tz=UTC").json()
    assert body["domain_min"] == "2026-03-10"
    assert body["domain_max"] == "2026-06-02"


def test_coverage_invalid_tz_422(client: Any) -> None:
    r = client.get("/inbox/coverage?tz=Marte/Olympus")
    assert r.status_code == 422


def test_coverage_negative_gap_422(client: Any) -> None:
    r = client.get("/inbox/coverage?gap_days=-1")
    assert r.status_code == 422


# ---- Tramos barridos (`swept`): bitácora ingest_swept_ranges + frontera del backfill_job --------


def _seed_swept(source_id: int, user_id: int, spans: list[tuple[str, str]]) -> None:
    """Inserta reclamos [start, end-exclusivo) en la bitácora ingest_swept_ranges."""
    with connection() as c:
        for start, end in spans:
            c.execute(
                text(
                    "INSERT INTO ingest_swept_ranges (user_id, source_id, range_start, range_end)"
                    " VALUES (:u, :s, :rs, :re)"
                ),
                {"u": user_id, "s": source_id, "rs": start, "re": end},
            )


def _seed_backfill_job(
    source_id: int, user_id: int, range_start: str, range_end: str, frontier: str
) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO backfill_jobs (user_id, source_id, range_start, range_end, frontier)"
                " VALUES (:u, :s, :rs, :re, :f)"
            ),
            {"u": user_id, "s": source_id, "rs": range_start, "re": range_end, "f": frontier},
        )


def test_coverage_swept_rows_merge_adjacent(client: Any, seed_source: dict[str, Any]) -> None:
    # [01..05) + [05..10) son adyacentes → un solo tramo 01..09; julio queda aparte.
    _seed_swept(
        seed_source["id"],
        1,
        [("2026-06-01", "2026-06-05"), ("2026-06-05", "2026-06-10"), ("2026-07-01", "2026-07-03")],
    )
    body = client.get("/inbox/coverage").json()
    lane = _lane(body, seed_source["id"])
    assert lane["swept"] == [
        {"start": "2026-06-01", "end": "2026-06-09", "days": 9},
        {"start": "2026-07-01", "end": "2026-07-02", "days": 2},
    ]
    assert lane["ranges"] == []
    assert lane["total"] == 0
    # El barrido también define el dominio del eje (barrido vacío = cobertura).
    assert body["domain_min"] == "2026-06-01"
    assert body["domain_max"] == "2026-07-02"


def test_coverage_swept_from_backfill_job_frontier(
    client: Any, seed_source: dict[str, Any]
) -> None:
    # Job vigente con frontera avanzada: lo ya recorrido [range_start, frontier) cuenta barrido.
    _seed_backfill_job(seed_source["id"], 1, "2026-01-01", "2026-04-01", "2026-02-01")
    other = _mk_source(1, "sin-avance", "imap")
    _seed_backfill_job(other, 1, "2026-01-01", "2026-04-01", "2026-01-01")  # frontera sin mover

    body = client.get("/inbox/coverage").json()
    assert _lane(body, seed_source["id"])["swept"] == [
        {"start": "2026-01-01", "end": "2026-01-31", "days": 31}
    ]
    assert _lane(body, other)["swept"] == []


def test_coverage_swept_merges_job_and_rows(client: Any, seed_source: dict[str, Any]) -> None:
    # La bitácora y la frontera del job se solapan → un solo tramo fundido.
    _seed_backfill_job(seed_source["id"], 1, "2026-01-01", "2026-06-01", "2026-02-01")
    _seed_swept(seed_source["id"], 1, [("2026-01-15", "2026-03-01")])
    lane = _lane(client.get("/inbox/coverage").json(), seed_source["id"])
    assert lane["swept"] == [{"start": "2026-01-01", "end": "2026-02-28", "days": 59}]


def test_coverage_swept_cross_tenant(
    client: Any, seed_source: dict[str, Any], seed_user2: int
) -> None:
    src2 = _mk_source(seed_user2, "ajena", "imap")
    _seed_swept(src2, seed_user2, [("2026-06-01", "2026-06-10")])
    body = client.get("/inbox/coverage").json()
    assert [ln["id"] for ln in body["lanes"]] == [seed_source["id"]]
    assert body["domain_min"] is None


# ---- record_swept_range (write-path compartido de run_fetch_window) -----------------------------


def _swept_rows(source_id: int) -> list[tuple[str, str]]:
    with connection() as c:
        rows = c.execute(
            text(
                "SELECT range_start, range_end FROM ingest_swept_ranges "
                "WHERE source_id = :s ORDER BY range_start"
            ),
            {"s": source_id},
        ).all()
    return [(str(r[0]), str(r[1])) for r in rows]


def test_record_swept_range_inserts(seed_source: dict[str, Any]) -> None:
    from memex.api.fetch_runner import record_swept_range

    record_swept_range(
        user_id=1,
        source_id=seed_source["id"],
        since="2026-01-01",
        until="2026-02-01",
        posted=0,
        limit=2000,
    )
    assert _swept_rows(seed_source["id"]) == [("2026-01-01", "2026-02-01")]


# ---- Ventana temporal (since/until) y cursor por lane -------------------------------------------


def _seed_checkpoint(source_id: int, cursor: str, updated_at: str) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO source_checkpoints (source_id, cursor, updated_at)"
                " VALUES (:s, CAST(:cur AS JSONB), :ts)"
            ),
            {"s": source_id, "cur": cursor, "ts": updated_at},
        )


def test_coverage_window_filters_and_fixes_domain(client: Any, seed_source: dict[str, Any]) -> None:
    _seed_at(
        seed_source["id"],
        1,
        [datetime(2026, m, d, 12, 0, tzinfo=UTC) for m, d in ((6, 1), (6, 15), (7, 10))],
    )
    body = client.get("/inbox/coverage?tz=UTC&since=2026-06-01&until=2026-06-30").json()
    lane = _lane(body, seed_source["id"])
    assert [r["start"] for r in lane["ranges"]] == ["2026-06-01", "2026-06-15"]
    assert lane["total"] == 2  # julio queda fuera
    # El eje ES la ventana pedida, no los extremos de los datos.
    assert body["domain_min"] == "2026-06-01"
    assert body["domain_max"] == "2026-06-30"


def test_coverage_window_clips_swept(client: Any, seed_source: dict[str, Any]) -> None:
    _seed_swept(seed_source["id"], 1, [("2026-05-20", "2026-06-10")])
    lane = _lane(
        client.get("/inbox/coverage?since=2026-06-01&until=2026-06-30").json(),
        seed_source["id"],
    )
    assert lane["swept"] == [{"start": "2026-06-01", "end": "2026-06-09", "days": 9}]


def test_coverage_window_inverted_422(client: Any) -> None:
    r = client.get("/inbox/coverage?since=2026-06-30&until=2026-06-01")
    assert r.status_code == 422


def test_coverage_window_since_only(client: Any, seed_source: dict[str, Any]) -> None:
    _seed_at(
        seed_source["id"],
        1,
        [datetime(2026, m, 10, 12, 0, tzinfo=UTC) for m in (3, 6)],
    )
    body = client.get("/inbox/coverage?tz=UTC&since=2026-05-01").json()
    lane = _lane(body, seed_source["id"])
    assert [r["start"] for r in lane["ranges"]] == ["2026-06-10"]
    assert body["domain_min"] == "2026-05-01"  # el piso pedido
    assert body["domain_max"] == "2026-06-10"  # el techo sale de los datos


def test_coverage_lane_cursor(client: Any, seed_source: dict[str, Any]) -> None:
    _seed_checkpoint(
        seed_source["id"],
        '{"folders": {"INBOX": {"uidvalidity": 17, "last_uid": 4321}}}',
        "2026-06-08T15:00:00Z",
    )
    body = client.get("/inbox/coverage?tz=UTC").json()
    lane = _lane(body, seed_source["id"])
    assert lane["cursor"] is not None
    assert lane["cursor"]["day"] == "2026-06-08"
    assert lane["cursor"]["summary"] == "1 carpeta(s) · uid hasta 4321"
    # El cursor también define dominio (única señal de esta lane).
    assert body["domain_min"] == "2026-06-08"

    # Ventana que excluye el día del cursor → marcador omitido.
    windowed = client.get("/inbox/coverage?tz=UTC&since=2026-01-01&until=2026-02-01").json()
    assert _lane(windowed, seed_source["id"])["cursor"] is None


def test_coverage_cursor_tz_day(client: Any, seed_source: dict[str, Any]) -> None:
    # 02:00Z del 9 de junio == 21:00 del 8 de junio en Bogotá: el día del marcador sigue la tz.
    _seed_checkpoint(seed_source["id"], '{"folders": {}}', "2026-06-09T02:00:00Z")
    bog = _lane(client.get("/inbox/coverage?tz=America/Bogota").json(), seed_source["id"])
    assert bog["cursor"]["day"] == "2026-06-08"
    utc = _lane(client.get("/inbox/coverage?tz=UTC").json(), seed_source["id"])
    assert utc["cursor"]["day"] == "2026-06-09"


# ---- record_swept_range: guards ------------------------------------------------------------------


def test_record_swept_range_skips_incomplete_windows(seed_source: dict[str, Any]) -> None:
    from memex.api.fetch_runner import record_swept_range

    sid = seed_source["id"]
    base: dict[str, Any] = {"user_id": 1, "source_id": sid, "posted": 0, "limit": 100}
    # Rango abierto (sin until) → no se reclama.
    record_swept_range(**{**base, "since": "2026-01-01", "until": None})
    # Posible truncamiento por cap (posted >= limit) → no se reclama.
    record_swept_range(
        user_id=1, source_id=sid, since="2026-01-01", until="2026-02-01", posted=100, limit=100
    )
    # Timestamps (no fechas puras) → no se reclama.
    record_swept_range(**{**base, "since": "2026-01-01T10:00:00", "until": "2026-01-02T10:00:00"})
    # Ventana vacía o invertida → no se reclama.
    record_swept_range(**{**base, "since": "2026-02-01", "until": "2026-02-01"})
    assert _swept_rows(sid) == []
