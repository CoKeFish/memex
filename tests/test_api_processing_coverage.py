"""GET /processing/coverage — de lo ingerido, qué días ya están MANEJADOS, por fuente.

Espejo de test_api_inbox_coverage.py pero para el timeline de procesamiento: "manejado" =
resumido / extraído / blacklist según `criterion`. Cubre: día completo vs parcial vs intacto,
blacklist como decisión tomada bajo todos los criterios, discriminación summarize/extract,
fusión por gap_days que NO puentea días con pendientes, ventana since/until, borde de TZ,
aislamiento multi-tenant, el marcador de frontera del lote (processing_lots) y los 422.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.core.inbox import insert_record
from memex.core.source import SourceRecord
from memex.db import connection


def _seed_at(source_id: int, user_id: int, whens: list[datetime], prefix: str = "r") -> list[int]:
    """Inserta mensajes y DEVUELVE sus inbox_ids (los seeds de manejo los necesitan)."""
    ids: list[int] = []
    with connection() as c:
        for i, occ in enumerate(whens):
            res = insert_record(
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
            assert res.id is not None
            ids.append(res.id)
    return ids


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


# ---- seeds de "manejado" -------------------------------------------------------------------


def _summarize(inbox_ids: list[int], user_id: int = 1) -> None:
    """Un summary batch que cubre todos los `inbox_ids` (vía summary_inbox_links)."""
    with connection() as c:
        summary_id = c.execute(
            text(
                "INSERT INTO summaries (user_id, tier, content) "
                "VALUES (:u, 'batch', 'resumen de prueba') RETURNING id"
            ),
            {"u": user_id},
        ).scalar()
        for iid in inbox_ids:
            c.execute(
                text("INSERT INTO summary_inbox_links (summary_id, inbox_id) VALUES (:s, :i)"),
                {"s": summary_id, "i": iid},
            )


def _extract(inbox_ids: list[int], user_id: int = 1) -> None:
    with connection() as c:
        for iid in inbox_ids:
            c.execute(
                text(
                    "INSERT INTO module_extractions (user_id, module_slug, inbox_id) "
                    "VALUES (:u, 'finance', :i)"
                ),
                {"u": user_id, "i": iid},
            )


def _classify(inbox_ids: list[int], tier: str, user_id: int = 1) -> None:
    with connection() as c:
        for iid in inbox_ids:
            c.execute(
                text("INSERT INTO classifications (user_id, inbox_id, tier) VALUES (:u, :i, :t)"),
                {"u": user_id, "i": iid, "t": tier},
            )


def _seed_lot(
    user_id: int, target_ids: list[int], frontier: int, *, source_id: int | None = None
) -> None:
    """Lote por ventanas (processing_lots) con la frontera ya avanzada a `frontier`."""
    config = {"filters": {"source_id": source_id}, "force": False}
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO processing_lots "
                "(user_id, stages, config, target_ids, frontier, window_size) "
                "VALUES (:u, :stages, CAST(:cfg AS JSONB), :tids, :f, 50)"
            ),
            {
                "u": user_id,
                "stages": ["summarize"],
                "cfg": json.dumps(config),
                "tids": target_ids,
                "f": frontier,
            },
        )


# ---- capas: completo / parcial / intacto ----------------------------------------------------


def test_full_partial_untouched_days(client: Any, seed_source: dict[str, Any]) -> None:
    # 1 jun: 2/2 manejados → banda sólida; 2 jun: 1/2 → parcial; 3 jun: 0/1 → no se pinta.
    d1 = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 1, 12, 0, tzinfo=UTC)] * 2, prefix="a")
    d2 = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 2, 12, 0, tzinfo=UTC)] * 2, prefix="b")
    _seed_at(seed_source["id"], 1, [datetime(2026, 6, 3, 12, 0, tzinfo=UTC)], prefix="c")
    _summarize(d1)
    _summarize(d2[:1])

    body = client.get("/processing/coverage?tz=UTC").json()
    lane = _lane(body, seed_source["id"])
    assert lane["ranges"] == [{"start": "2026-06-01", "end": "2026-06-01", "days": 1, "count": 2}]
    assert lane["swept"] == [{"start": "2026-06-02", "end": "2026-06-02", "days": 1}]
    assert lane["total"] == 3  # manejados (2 del día completo + 1 del parcial), no lo ingerido
    assert lane["first_day"] == "2026-06-01"
    assert lane["last_day"] == "2026-06-01"
    # El día intacto no pinta ni aporta dominio.
    assert body["domain_min"] == "2026-06-01"
    assert body["domain_max"] == "2026-06-02"


def test_coverage_empty(client: Any, seed_source: dict[str, Any]) -> None:
    body = client.get("/processing/coverage").json()
    lane = _lane(body, seed_source["id"])
    assert lane["ranges"] == []
    assert lane["swept"] == []
    assert lane["total"] == 0
    assert lane["cursor"] is None
    assert body["domain_min"] is None
    # Defaults espejados en la respuesta (paridad con /inbox/coverage).
    assert body["tz"] == "America/Bogota"
    assert body["gap_days"] == 2


def test_partial_adjacent_days_merge(client: Any, seed_source: dict[str, Any]) -> None:
    # Días 1 y 2 parciales (1 de 2 manejado cada uno) → UN solo span tenue de 2 días.
    d1 = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 1, 12, 0, tzinfo=UTC)] * 2, prefix="a")
    d2 = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 2, 12, 0, tzinfo=UTC)] * 2, prefix="b")
    _summarize([d1[0], d2[0]])

    lane = _lane(client.get("/processing/coverage?tz=UTC").json(), seed_source["id"])
    assert lane["ranges"] == []
    assert lane["swept"] == [{"start": "2026-06-01", "end": "2026-06-02", "days": 2}]


def test_gap_fusion_bridges_empty_days_not_pending(
    client: Any, seed_source: dict[str, Any]
) -> None:
    # Días 1 y 3 completos, día 2 SIN mensajes → gap_days=2 los funde en un tramo.
    d1 = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 1, 12, 0, tzinfo=UTC)], prefix="a")
    d3 = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 3, 12, 0, tzinfo=UTC)], prefix="b")
    _summarize(d1 + d3)
    lane = _lane(client.get("/processing/coverage?tz=UTC&gap_days=2").json(), seed_source["id"])
    assert lane["ranges"] == [{"start": "2026-06-01", "end": "2026-06-03", "days": 3, "count": 2}]

    # Aparece un PENDIENTE el día 2 → la banda sólida no puede taparlo: dos tramos separados.
    _seed_at(seed_source["id"], 1, [datetime(2026, 6, 2, 12, 0, tzinfo=UTC)], prefix="p")
    lane = _lane(client.get("/processing/coverage?tz=UTC&gap_days=2").json(), seed_source["id"])
    assert lane["ranges"] == [
        {"start": "2026-06-01", "end": "2026-06-01", "days": 1, "count": 1},
        {"start": "2026-06-03", "end": "2026-06-03", "days": 1, "count": 1},
    ]
    assert lane["swept"] == []  # 0 manejados ese día: pendiente, no parcial


# ---- criterios ------------------------------------------------------------------------------


def test_blacklist_counts_under_all_criteria(client: Any, seed_source: dict[str, Any]) -> None:
    ids = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 1, 12, 0, tzinfo=UTC)], prefix="bl")
    _classify(ids, "blacklist")
    for crit in ("any", "summarize", "extract"):
        lane = _lane(
            client.get(f"/processing/coverage?tz=UTC&criterion={crit}").json(),
            seed_source["id"],
        )
        assert lane["ranges"] == [
            {"start": "2026-06-01", "end": "2026-06-01", "days": 1, "count": 1}
        ], crit


def test_criterion_discriminates_stage(client: Any, seed_source: dict[str, Any]) -> None:
    # Un mensaje SOLO resumido (1 jun) y otro SOLO extraído (5 jun).
    a = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 1, 12, 0, tzinfo=UTC)], prefix="s")
    b = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 5, 12, 0, tzinfo=UTC)], prefix="e")
    _summarize(a)
    _extract(b)

    summ = _lane(
        client.get("/processing/coverage?tz=UTC&criterion=summarize").json(), seed_source["id"]
    )
    assert [r["start"] for r in summ["ranges"]] == ["2026-06-01"]
    assert summ["total"] == 1

    extr = _lane(
        client.get("/processing/coverage?tz=UTC&criterion=extract").json(), seed_source["id"]
    )
    assert [r["start"] for r in extr["ranges"]] == ["2026-06-05"]
    assert extr["total"] == 1

    both = _lane(client.get("/processing/coverage?tz=UTC").json(), seed_source["id"])  # any
    assert [r["start"] for r in both["ranges"]] == ["2026-06-01", "2026-06-05"]
    assert both["total"] == 2


def test_batch_tier_is_not_handled(client: Any, seed_source: dict[str, Any]) -> None:
    # batch/individual = "en el pipeline", no decisión final: el día queda pendiente.
    ids = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 1, 12, 0, tzinfo=UTC)], prefix="q")
    _classify(ids, "batch")
    lane = _lane(client.get("/processing/coverage?tz=UTC").json(), seed_source["id"])
    assert lane["ranges"] == []
    assert lane["swept"] == []
    assert lane["total"] == 0


# ---- ventana, tz y validación ---------------------------------------------------------------


def test_window_filters_and_fixes_domain(client: Any, seed_source: dict[str, Any]) -> None:
    ids = _seed_at(
        seed_source["id"],
        1,
        [datetime(2026, m, d, 12, 0, tzinfo=UTC) for m, d in ((6, 1), (6, 15), (7, 10))],
    )
    _summarize(ids)
    body = client.get("/processing/coverage?tz=UTC&since=2026-06-01&until=2026-06-30").json()
    lane = _lane(body, seed_source["id"])
    assert [r["start"] for r in lane["ranges"]] == ["2026-06-01", "2026-06-15"]
    assert lane["total"] == 2  # julio queda fuera
    # El eje ES la ventana pedida, no los extremos de los datos.
    assert body["domain_min"] == "2026-06-01"
    assert body["domain_max"] == "2026-06-30"


def test_tz_boundary(client: Any, seed_source: dict[str, Any]) -> None:
    # 2026-06-02 04:30Z == 2026-06-01 23:30 en Bogotá: el día manejado sigue la tz pedida.
    ids = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 2, 4, 30, tzinfo=UTC)])
    _summarize(ids)
    bog = _lane(client.get("/processing/coverage?tz=America/Bogota").json(), seed_source["id"])
    assert bog["ranges"][0]["start"] == "2026-06-01"
    utc = _lane(client.get("/processing/coverage?tz=UTC").json(), seed_source["id"])
    assert utc["ranges"][0]["start"] == "2026-06-02"


def test_window_inverted_422(client: Any) -> None:
    r = client.get("/processing/coverage?since=2026-06-30&until=2026-06-01")
    assert r.status_code == 422


def test_invalid_tz_422(client: Any) -> None:
    assert client.get("/processing/coverage?tz=Marte/Olympus").status_code == 422


def test_invalid_criterion_422(client: Any) -> None:
    assert client.get("/processing/coverage?criterion=delete").status_code == 422


# ---- filtros kind / source_id ----------------------------------------------------------------


def test_kind_and_source_filters(client: Any, seed_source: dict[str, Any]) -> None:
    chat_id = _mk_source(1, "tg-test", "telegram")
    a = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 1, 12, 0, tzinfo=UTC)], prefix="a")
    b = _seed_at(chat_id, 1, [datetime(2026, 6, 2, 12, 0, tzinfo=UTC)], prefix="b")
    _summarize(a + b)

    body = client.get("/processing/coverage?tz=UTC&kind=chat").json()
    assert [ln["id"] for ln in body["lanes"]] == [chat_id]
    # El dominio se calcula sobre las lanes filtradas, no sobre todo el inbox.
    assert body["domain_min"] == "2026-06-02"

    body = client.get(f"/processing/coverage?tz=UTC&source_id={seed_source['id']}").json()
    assert [ln["id"] for ln in body["lanes"]] == [seed_source["id"]]
    assert body["domain_max"] == "2026-06-01"


# ---- multi-tenant ----------------------------------------------------------------------------


def test_cross_tenant(client: Any, seed_source: dict[str, Any], seed_user2: int) -> None:
    src2 = _mk_source(seed_user2, "ajena", "imap")
    ids2 = _seed_at(src2, seed_user2, [datetime(2026, 6, 1, 12, 0, tzinfo=UTC)], prefix="u2-")
    _summarize(ids2, user_id=seed_user2)
    _seed_lot(seed_user2, ids2, 1)

    body = client.get("/processing/coverage").json()
    assert [ln["id"] for ln in body["lanes"]] == [seed_source["id"]]
    assert body["domain_min"] is None  # ni los datos ni el lote del otro usuario aportan
    assert _lane(body, seed_source["id"])["cursor"] is None


# ---- marcador: frontera del lote -------------------------------------------------------------


def test_lot_marker(client: Any, seed_source: dict[str, Any]) -> None:
    ids = _seed_at(
        seed_source["id"],
        1,
        [datetime(2026, 6, d, 12, 0, tzinfo=UTC) for d in (1, 3, 5)],
        prefix="t",
    )
    _seed_lot(1, ids, frontier=2)  # último procesado por el lote = ids[1] (3 jun)

    body = client.get("/processing/coverage?tz=UTC").json()
    lane = _lane(body, seed_source["id"])
    assert lane["cursor"] is not None
    assert lane["cursor"]["day"] == "2026-06-03"
    assert lane["cursor"]["summary"] == "lote: 2/3 mensajes"
    # El marcador también define dominio (única señal: nada manejado todavía).
    assert body["domain_min"] == "2026-06-03"
    assert body["domain_max"] == "2026-06-03"

    # Ventana que excluye el día de la frontera → marcador omitido.
    windowed = client.get("/processing/coverage?tz=UTC&since=2026-07-01&until=2026-07-31").json()
    assert _lane(windowed, seed_source["id"])["cursor"] is None


def test_lot_marker_frontier_zero_absent(client: Any, seed_source: dict[str, Any]) -> None:
    ids = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 1, 12, 0, tzinfo=UTC)], prefix="t")
    _seed_lot(1, ids, frontier=0)  # configurado pero sin avanzar: no hay "va por acá"
    lane = _lane(client.get("/processing/coverage?tz=UTC").json(), seed_source["id"])
    assert lane["cursor"] is None


def test_lot_marker_source_scoped_vs_global(client: Any, seed_source: dict[str, Any]) -> None:
    other = _mk_source(1, "otra", "telegram")
    ids = _seed_at(seed_source["id"], 1, [datetime(2026, 6, 3, 12, 0, tzinfo=UTC)], prefix="t")

    # Lote filtrado a una fuente → el marcador va SOLO en esa lane.
    _seed_lot(1, ids, frontier=1, source_id=seed_source["id"])
    body = client.get("/processing/coverage?tz=UTC").json()
    assert _lane(body, seed_source["id"])["cursor"] is not None
    assert _lane(body, other)["cursor"] is None

    # Lote global (sin filtro de fuente) → la frontera es temporal: va en TODAS las lanes.
    with connection() as c:
        c.execute(text("DELETE FROM processing_lots WHERE user_id = 1"))
    _seed_lot(1, ids, frontier=1)
    body = client.get("/processing/coverage?tz=UTC").json()
    assert _lane(body, seed_source["id"])["cursor"] is not None
    assert _lane(body, other)["cursor"] is not None
