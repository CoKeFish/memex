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
