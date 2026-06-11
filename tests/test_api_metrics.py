from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.db import connection


def _seed_source(name: str, stype: str = "imap", user_id: int = 1) -> int:
    with connection() as c:
        return int(
            c.execute(
                text("INSERT INTO sources (user_id, name, type) VALUES (:u, :n, :t) RETURNING id"),
                {"u": user_id, "n": name, "t": stype},
            ).scalar_one()
        )


def _seed_llm_call(
    user_id: int = 1,
    *,
    purpose: str = "summarize_batch",
    model: str = "deepseek-chat",
    prompt_tokens: int = 1000,
    completion_tokens: int = 200,
    cache_hit_tokens: int = 0,
    cost_usd: str = "0.01",
    latency_ms: int = 300,
    status: str = "ok",
    inbox_id: int | None = None,
    source_id: int | None = None,
    request_id: str | None = None,
    error_message: str | None = None,
    created_at: datetime | None = None,
    metadata: str = "{}",
) -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO llm_calls
                  (user_id, request_id, inbox_id, source_id, purpose, model, prompt_tokens,
                   completion_tokens, cache_hit_tokens, cost_usd, latency_ms, status,
                   error_message, created_at, metadata)
                VALUES
                  (:uid, :rid, :inbox, :src, :purpose, :model, :pt, :ct, :cht, :cost, :lat,
                   :status, :err, COALESCE(:created_at, NOW()), CAST(:meta AS JSONB))
                """
            ),
            {
                "uid": user_id,
                "rid": request_id,
                "inbox": inbox_id,
                "src": source_id,
                "purpose": purpose,
                "model": model,
                "pt": prompt_tokens,
                "ct": completion_tokens,
                "cht": cache_hit_tokens,
                "cost": cost_usd,
                "lat": latency_ms,
                "status": status,
                "err": error_message,
                "created_at": created_at,
                "meta": metadata,
            },
        )


# ---- rollup: KPIs ------------------------------------------------------------------------------


def test_rollup_kpis_totals(client: Any) -> None:
    _seed_llm_call(prompt_tokens=1000, completion_tokens=100, cache_hit_tokens=400, cost_usd="0.02")
    _seed_llm_call(prompt_tokens=2000, completion_tokens=300, cache_hit_tokens=600, cost_usd="0.03")
    _seed_llm_call(status="error", cost_usd="0", prompt_tokens=0, completion_tokens=0, latency_ms=0)
    k = client.get("/metrics/llm/rollup").json()["kpis"]
    assert k["calls"] == 3
    assert k["cost_usd"] == 0.05
    assert isinstance(k["cost_usd"], float)
    assert k["prompt_tokens"] == 3000
    assert k["completion_tokens"] == 400
    assert k["cache_hit_tokens"] == 1000
    assert abs(k["cache_hit_ratio"] - (1000 / 3000)) < 1e-9
    assert k["errors"] == 1
    assert abs(k["avg_cost_usd"] - (0.05 / 3)) < 1e-9


def test_rollup_avg_latency_excludes_non_ok(client: Any) -> None:
    # avg_latency_ms promedia SOLO status='ok'; los filtered/error de latencia 0 no son llamadas
    # reales al LLM y diluirían el promedio.
    _seed_llm_call(status="ok", latency_ms=400)
    _seed_llm_call(status="filtered", latency_ms=0, cost_usd="0")
    _seed_llm_call(status="error", latency_ms=0, cost_usd="0")
    k = client.get("/metrics/llm/rollup").json()["kpis"]
    assert k["avg_latency_ms"] == 400.0


def test_rollup_prev_window(client: Any) -> None:
    # Ventana [05-20, 05-27); previa [05-13, 05-20).
    _seed_llm_call(cost_usd="0.05", created_at=datetime(2026, 5, 22, 12, tzinfo=UTC))
    _seed_llm_call(cost_usd="0.03", created_at=datetime(2026, 5, 15, 12, tzinfo=UTC))
    body = client.get(
        "/metrics/llm/rollup?since=2026-05-20T00:00:00Z&until=2026-05-27T00:00:00Z"
    ).json()
    assert body["kpis"]["cost_usd"] == 0.05
    assert body["kpis"]["prev_cost_usd"] == 0.03
    # Sin `since` (todo el tiempo) → no hay variación.
    assert client.get("/metrics/llm/rollup").json()["kpis"]["prev_cost_usd"] is None


def test_rollup_prev_calls(client: Any) -> None:
    # prev_calls cuenta las filas del periodo previo (distingue "previo vacío" de "creció mucho").
    _seed_llm_call(cost_usd="0.05", created_at=datetime(2026, 5, 22, 12, tzinfo=UTC))  # en ventana
    _seed_llm_call(cost_usd="0.03", created_at=datetime(2026, 5, 15, 12, tzinfo=UTC))  # en previa
    k = client.get(
        "/metrics/llm/rollup?since=2026-05-20T00:00:00Z&until=2026-05-27T00:00:00Z"
    ).json()["kpis"]
    assert k["prev_calls"] == 1
    # Ventana [05-13, 05-20): su periodo previo [05-06, 05-13) no tiene datos → 0 (no None).
    k2 = client.get(
        "/metrics/llm/rollup?since=2026-05-13T00:00:00Z&until=2026-05-20T00:00:00Z"
    ).json()["kpis"]
    assert k2["prev_calls"] == 0
    # Sin `since` → None.
    assert client.get("/metrics/llm/rollup").json()["kpis"]["prev_calls"] is None


def test_rollup_cross_tenant(client: Any, seed_user2: int) -> None:
    _seed_llm_call(1, cost_usd="0.10")
    _seed_llm_call(seed_user2, cost_usd="0.99")
    k = client.get("/metrics/llm/rollup").json()["kpis"]
    assert k["calls"] == 1
    assert k["cost_usd"] == 0.10


# ---- rollup: por módulo (derivado de purpose) --------------------------------------------------


def test_rollup_by_module_mapping(client: Any) -> None:
    cases = {
        "module_route": "routing",
        "summarize_batch": "summarize",
        "summarize_individual": "summarize",
        "extract_finance": "finance",
        "extract_grouped": "grouped",
        "calendar_merge": "calendar",
        "calendar_dedup": "calendar",
        "finance_dedup": "finance",  # fase interna → se pliega a su módulo
        "identidades_dedup": "identidades",
        "identidades_cooccurrence": "identidades",
        "identidades_hierarchy": "identidades",
        "ocr": "ocr",
        "extract_health": "health",  # purpose futuro → cae al ELSE, nombrado por su slug
        "graph_cluster_partition": "graph_cluster_partition",  # subsistema sin módulo → ELSE
    }
    for purpose in cases:
        _seed_llm_call(purpose=purpose, cost_usd="0.01")
    body = client.get("/metrics/llm/rollup").json()
    by_module = {r["module"]: r for r in body["by_module"]}
    assert set(by_module) == set(cases.values())
    assert by_module["summarize"]["calls"] == 2  # batch + individual
    assert by_module["calendar"]["calls"] == 2  # merge + dedup
    assert by_module["finance"]["calls"] == 2  # extracción + dedup
    assert by_module["identidades"]["calls"] == 3  # dedup + cooccurrence + hierarchy
    assert set(body["modules"]) == set(cases.values())


# ---- rollup: por fuente -------------------------------------------------------------------------


def test_rollup_by_source_labels(client: Any) -> None:
    src = _seed_source("Gmail personal")
    _seed_llm_call(purpose="extract_finance", source_id=src, cost_usd="0.04")
    _seed_llm_call(purpose="extract_finance", source_id=src, cost_usd="0.02")
    _seed_llm_call(purpose="calendar_merge", source_id=None, cost_usd="0.03")  # → (calendar)
    _seed_llm_call(purpose="summarize_batch", source_id=None, cost_usd="0.01")  # → (sin source)
    by_source = {r["source_name"]: r for r in client.get("/metrics/llm/rollup").json()["by_source"]}
    assert by_source["Gmail personal"]["calls"] == 2
    assert by_source["Gmail personal"]["cost_usd"] == 0.06
    assert "(calendar)" in by_source
    assert "(sin source)" in by_source
    assert by_source["(calendar)"]["cost_usd"] == 0.03
    assert by_source["(sin source)"]["cost_usd"] == 0.01


# ---- rollup: por modelo (untabulated) ----------------------------------------------------------


def test_rollup_by_model_untabulated(client: Any) -> None:
    _seed_llm_call(model="deepseek-chat", cost_usd="0.02", prompt_tokens=1000)
    _seed_llm_call(model="vision-x", cost_usd="0", prompt_tokens=5000, completion_tokens=10)  # gap
    by_model = {r["model"]: r for r in client.get("/metrics/llm/rollup").json()["by_model"]}
    assert by_model["deepseek-chat"]["untabulated"] is False
    assert by_model["vision-x"]["untabulated"] is True


# ---- rollup: matriz + serie diaria -------------------------------------------------------------


def test_rollup_source_module_matrix_and_daily(client: Any) -> None:
    src = _seed_source("Telegram")
    _seed_llm_call(
        purpose="extract_finance",
        source_id=src,
        cost_usd="0.02",
        created_at=datetime(2026, 5, 10, 12, tzinfo=UTC),
    )
    _seed_llm_call(
        purpose="ocr",
        source_id=src,
        cost_usd="0.01",
        created_at=datetime(2026, 5, 12, 12, tzinfo=UTC),
    )
    body = client.get("/metrics/llm/rollup").json()
    # Matriz: dos celdas (finance, ocr) para la fuente Telegram.
    cells = {(c["source_name"], c["module"]): c for c in body["by_source_module"]}
    assert cells[("Telegram", "finance")]["cost_usd"] == 0.02
    assert cells[("Telegram", "ocr")]["cost_usd"] == 0.01
    # Serie diaria: dos días distintos.
    days = {d["day"]: d for d in body["daily"]}
    assert "2026-05-10" in days
    assert "2026-05-12" in days
    assert days["2026-05-10"]["by_module"]["finance"] == 0.02
    assert days["2026-05-12"]["total"] == 0.01


# ---- rollup: TZ del bucket diario --------------------------------------------------------------


def test_rollup_daily_bucket_respects_tz(client: Any) -> None:
    # 2026-06-02T04:00Z = 2026-06-01 23:00 en Bogotá (UTC-5); el bucket debe seguir la TZ pedida.
    _seed_llm_call(cost_usd="0.01", created_at=datetime(2026, 6, 2, 4, tzinfo=UTC))
    bogota = {d["day"] for d in client.get("/metrics/llm/rollup?tz=America/Bogota").json()["daily"]}
    assert bogota == {"2026-06-01"}
    utc = {d["day"] for d in client.get("/metrics/llm/rollup?tz=UTC").json()["daily"]}
    assert utc == {"2026-06-02"}


def test_rollup_tz_invalid_returns_422(client: Any) -> None:
    assert client.get("/metrics/llm/rollup?tz=Marte/Olympus").status_code == 422
    assert client.get("/metrics/llm/calls?tz=Marte/Olympus").status_code == 422


# ---- auditoría: filtros incluir/excluir --------------------------------------------------------


def test_audit_filter_status(client: Any) -> None:
    _seed_llm_call(status="ok")
    _seed_llm_call(status="error", error_message="boom")
    _seed_llm_call(status="filtered")
    only_err = client.get("/metrics/llm/calls?status=error").json()
    assert only_err["total"] == 1
    assert only_err["items"][0]["status"] == "error"
    assert only_err["items"][0]["error_message"] == "boom"
    # excluir error → 2 (ok + filtered)
    excl = client.get("/metrics/llm/calls?status=error&status_mode=exclude").json()
    assert excl["total"] == 2
    assert all(i["status"] != "error" for i in excl["items"])


def test_audit_filter_module_and_source(client: Any) -> None:
    src = _seed_source("Gmail")
    _seed_llm_call(purpose="extract_finance", source_id=src)
    _seed_llm_call(purpose="ocr", source_id=src)
    _seed_llm_call(purpose="calendar_merge", source_id=None)
    fin = client.get("/metrics/llm/calls?module=finance").json()
    assert fin["total"] == 1
    assert fin["items"][0]["module"] == "finance"
    # filtro por source_name (incluye el pseudo "(calendar)")
    cal = client.get("/metrics/llm/calls?source=(calendar)").json()
    assert cal["total"] == 1
    assert cal["items"][0]["module"] == "calendar"
    gmail = client.get("/metrics/llm/calls?source=Gmail").json()
    assert gmail["total"] == 2


def test_audit_filter_multi_value_exclude(client: Any) -> None:
    _seed_llm_call(purpose="ocr")
    _seed_llm_call(purpose="module_route")
    _seed_llm_call(purpose="extract_finance")
    # excluir ocr y routing → solo finance
    r = client.get("/metrics/llm/calls?module=ocr&module=routing&module_mode=exclude").json()
    assert r["total"] == 1
    assert r["items"][0]["module"] == "finance"


def test_audit_search_q(client: Any) -> None:
    _seed_llm_call(request_id="abc123", purpose="ocr")
    _seed_llm_call(request_id="zzz999", purpose="summarize_batch")
    assert client.get("/metrics/llm/calls?q=abc").json()["total"] == 1
    assert client.get("/metrics/llm/calls?q=summarize").json()["total"] == 1  # match en purpose


# ---- auditoría: orden + paginación -------------------------------------------------------------


def test_audit_sort_by_cost(client: Any) -> None:
    _seed_llm_call(cost_usd="0.01")
    _seed_llm_call(cost_usd="0.05")
    _seed_llm_call(cost_usd="0.03")
    desc = [
        i["cost_usd"]
        for i in client.get("/metrics/llm/calls?sort=cost_usd&dir=desc").json()["items"]
    ]
    assert desc == [0.05, 0.03, 0.01]
    asc = [
        i["cost_usd"]
        for i in client.get("/metrics/llm/calls?sort=cost_usd&dir=asc").json()["items"]
    ]
    assert asc == [0.01, 0.03, 0.05]


def test_audit_sort_by_latency(client: Any) -> None:
    _seed_llm_call(latency_ms=100)
    _seed_llm_call(latency_ms=9000)
    _seed_llm_call(latency_ms=500)
    top = client.get("/metrics/llm/calls?sort=latency_ms&dir=desc&limit=1").json()
    assert top["items"][0]["latency_ms"] == 9000


def test_audit_pagination(client: Any) -> None:
    for _ in range(5):
        _seed_llm_call()
    p0 = client.get("/metrics/llm/calls?limit=2&offset=0").json()
    assert p0["total"] == 5
    assert len(p0["items"]) == 2
    p2 = client.get("/metrics/llm/calls?limit=2&offset=4").json()
    assert p2["total"] == 5
    assert len(p2["items"]) == 1


def test_audit_empty(client: Any) -> None:
    body = client.get("/metrics/llm/calls").json()
    assert body == {"items": [], "total": 0}


def test_llm_call_detail_includes_response_text(client: Any) -> None:
    """El detalle por-llamada devuelve `response_text` (texto crudo del LLM) — que el list omite — y
    deriva `module` de `purpose`."""
    with connection() as c:
        call_id = int(
            c.execute(
                text(
                    """
                    INSERT INTO llm_calls
                      (user_id, purpose, model, prompt_tokens, completion_tokens, cache_hit_tokens,
                       cost_usd, latency_ms, status, metadata, response_text)
                    VALUES (1, 'extract_finance', 'deepseek-chat', 10, 5, 0, 0.001, 100, 'ok',
                            '{}'::jsonb, :rt)
                    RETURNING id
                    """
                ),
                {"rt": '{"items": []}'},
            ).scalar_one()
        )

    body = client.get(f"/metrics/llm/calls/{call_id}").json()
    assert body["id"] == call_id
    assert body["response_text"] == '{"items": []}'
    assert body["module"] == "finance"  # derivado de purpose


def test_llm_call_detail_missing_returns_404(client: Any) -> None:
    assert client.get("/metrics/llm/calls/999999").status_code == 404


# ---- Apify: rollup + auditoría de runs ----------------------------------------------------------


def _seed_apify_run(
    user_id: int = 1,
    *,
    source_id: int | None = None,
    platform: str = "x",
    account: str = "nasa",
    status: str = "ok",
    items_scraped: int = 2,
    items_kept: int = 1,
    cost_usd: str | None = "0.0008",
    charged_events: str | None = None,
) -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO apify_runs
                  (user_id, source_id, platform, account, actor_id, apify_run_id, status,
                   items_scraped, items_kept, cost_usd, charged_events)
                VALUES
                  (:uid, :src, :p, :a, 'apidojo/tweet-scraper', 'RX', :st, :isc, :ik, :cost,
                   CAST(:ce AS JSONB))
                """
            ),
            {
                "uid": user_id,
                "src": source_id,
                "p": platform,
                "a": account,
                "st": status,
                "isc": items_scraped,
                "ik": items_kept,
                "cost": cost_usd,
                "ce": charged_events,
            },
        )


def test_apify_rollup_aggregates(client: Any) -> None:
    sid = _seed_source("apify-mx", "x")
    _seed_apify_run(source_id=sid, cost_usd="0.0008", platform="x", account="nasa")
    _seed_apify_run(source_id=sid, cost_usd="0.0004", platform="x", account="spacex")
    _seed_apify_run(source_id=sid, cost_usd=None, status="error", platform="instagram")
    body = client.get("/metrics/apify/rollup").json()
    k = body["kpis"]
    assert k["runs"] == 3
    assert round(k["cost_usd"], 6) == 0.0012
    assert k["errors"] == 1
    assert k["accounts"] == 3  # (x,nasa) (x,spacex) (instagram,nasa)
    assert {b["platform"] for b in body["by_platform"]} == {"x", "instagram"}
    assert ("x", "nasa") in {(b["platform"], b["account"]) for b in body["by_account"]}
    assert body["by_source"][0]["source_name"] == "apify-mx"
    assert len(body["daily"]) >= 1
    assert body["daily"][0]["total"] > 0


def test_apify_rollup_scopes_by_user(client: Any, seed_user2: int) -> None:
    _seed_apify_run(user_id=seed_user2, cost_usd="9.0")
    assert client.get("/metrics/apify/rollup").json()["kpis"]["runs"] == 0


def test_apify_runs_audit_filters_and_paginates(client: Any) -> None:
    sid = _seed_source("apify-audit", "x")
    for i in range(3):
        _seed_apify_run(source_id=sid, account=f"acc{i}")
    _seed_apify_run(
        source_id=sid,
        account="other",
        status="timeout",
        cost_usd=None,
        charged_events='{"result": 7}',
    )
    body = client.get("/metrics/apify/runs", params={"status": ["timeout"]}).json()
    assert body["total"] == 1
    row = body["items"][0]
    assert row["status"] == "timeout"
    assert row["cost_usd"] is None
    assert row["charged_events"] == {"result": 7}
    assert row["source_name"] == "apify-audit"
    page = client.get("/metrics/apify/runs", params={"limit": 2}).json()
    assert page["total"] == 4
    assert len(page["items"]) == 2
    assert client.get("/metrics/apify/runs", params={"q": "acc1"}).json()["total"] == 1


def test_apify_cost_survives_source_deletion(client: Any) -> None:
    sid = _seed_source("apify-bye", "x")
    _seed_apify_run(source_id=sid, cost_usd="0.001")
    r = client.delete(f"/sources/{sid}")
    assert r.status_code in (200, 204)
    body = client.get("/metrics/apify/rollup").json()
    rows = [b for b in body["by_source"] if b["source_name"] == "(fuente borrada)"]
    assert rows
    assert rows[0]["source_id"] is None
    assert rows[0]["cost_usd"] == 0.001
