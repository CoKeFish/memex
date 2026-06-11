"""Tests del router de SOLO LECTURA `/calendar` (espeja `test_api_finance.py`).

Siembra filas en las tablas `mod_calendar_*` (limpiadas entre tests por el TRUNCATE ... users
CASCADE de `_reset_tables`) y verifica el shape que consume el dashboard, el scoping por usuario
y que NUNCA se filtra el token del proveedor.
"""

from __future__ import annotations

from datetime import date, time
from typing import Any

from sqlalchemy import text

from memex.db import connection


def _seed_event(
    user_id: int,
    *,
    title: str = "Evento",
    starts_on: date = date(2026, 6, 12),
    start_time: time | None = time(9, 0),
    location: str = "",
    origin: str = "extraction",
    provider: str | None = None,
    protected: bool = False,
    priority_rank: int = 0,
    processing_outcome: str = "unique",
    source_inbox_ids: list[int] | None = None,
    evidence: str = "",
    recurring_event_id: str | None = None,
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_calendar_events
                      (user_id, source_inbox_ids, title, starts_on, start_time, location, origin,
                       provider, protected, priority_rank, processing_outcome, evidence,
                       recurring_event_id)
                    VALUES
                      (:uid, :ids, :title, :starts_on, :start_time, :location, :origin,
                       :provider, :protected, :rank, :outcome, :evidence, :rec)
                    RETURNING id
                    """
                ),
                {
                    "uid": user_id,
                    "ids": source_inbox_ids if source_inbox_ids is not None else [],
                    "title": title,
                    "starts_on": starts_on,
                    "start_time": start_time,
                    "location": location,
                    "origin": origin,
                    "provider": provider,
                    "protected": protected,
                    "rank": priority_rank,
                    "outcome": processing_outcome,
                    "evidence": evidence,
                    "rec": recurring_event_id,
                },
            ).scalar_one()
        )


def _seed_consolidated(
    user_id: int,
    *,
    winner_event_id: int | None = None,
    title: str = "Evento",
    starts_on: date = date(2026, 6, 12),
    start_time: time | None = time(9, 0),
    location: str = "",
    deleted: bool = False,
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_calendar_consolidated
                      (user_id, title, starts_on, start_time, location, winner_event_id, deleted,
                       deleted_source)
                    VALUES (:uid, :title, :starts_on, :start_time, :location, :winner, :deleted,
                            CASE WHEN :deleted THEN 'user' END)
                    RETURNING id
                    """
                ),
                {
                    "uid": user_id,
                    "title": title,
                    "starts_on": starts_on,
                    "start_time": start_time,
                    "location": location,
                    "winner": winner_event_id,
                    "deleted": deleted,
                },
            ).scalar_one()
        )


def _link(user_id: int, consolidated_id: int, event_id: int) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_calendar_event_links (user_id, consolidated_id, event_id) "
                "VALUES (:uid, :cid, :eid)"
            ),
            {"uid": user_id, "cid": consolidated_id, "eid": event_id},
        )


def _seed_provider_account(
    user_id: int,
    *,
    provider: str = "google",
    account_label: str = "Personal",
    token_path_env: str = "GOOGLE_CALENDAR_TOKEN_PATH",
    sync_token: str | None = "CAES-cursor-opaco",
    write_back: bool = True,
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_calendar_provider_accounts
                      (user_id, provider, account_label, token_path_env, sync_token, write_back)
                    VALUES (:uid, :provider, :label, :token_env, :sync_token, :wb)
                    RETURNING id
                    """
                ),
                {
                    "uid": user_id,
                    "provider": provider,
                    "label": account_label,
                    "token_env": token_path_env,
                    "sync_token": sync_token,
                    "wb": write_back,
                },
            ).scalar_one()
        )


# ---- /calendar/events ----------------------------------------------------------------------------


def test_list_events_returns_consolidated_with_members(client: Any) -> None:
    winner = _seed_event(1, origin="provider", provider="google", protected=True, priority_rank=100)
    other = _seed_event(1, origin="extraction", source_inbox_ids=[7, 8])
    cid = _seed_consolidated(1, winner_event_id=winner, title="Vuelo BOG → MEX")
    _link(1, cid, winner)
    _link(1, cid, other)

    body = client.get("/calendar/events").json()
    assert len(body["items"]) == 1
    ev = body["items"][0]
    assert ev["title"] == "Vuelo BOG → MEX"
    assert ev["member_count"] == 2
    assert ev["protected"] is True  # del ganador
    assert ev["priority_rank"] == 100
    assert set(ev["origins"]) == {"provider", "extraction"}
    assert ev["start_time"] == "09:00:00"
    winners = [m for m in ev["members"] if m["is_winner"]]
    assert len(winners) == 1 and winners[0]["id"] == winner
    extraction_member = next(m for m in ev["members"] if m["origin"] == "extraction")
    assert extraction_member["source_inbox_ids"] == [7, 8]


def test_list_events_excludes_deleted(client: Any) -> None:
    e = _seed_event(1)
    _seed_consolidated(1, winner_event_id=e, deleted=True)
    assert client.get("/calendar/events").json()["items"] == []


def test_list_events_cross_tenant_scoped(client: Any, seed_user2: int) -> None:
    e1 = _seed_event(1, title="mío")
    _seed_consolidated(1, winner_event_id=e1, title="mío")
    e2 = _seed_event(seed_user2, title="ajeno")
    _seed_consolidated(seed_user2, winner_event_id=e2, title="ajeno")
    items = client.get("/calendar/events").json()["items"]
    assert len(items) == 1 and items[0]["title"] == "mío"


def test_list_events_pagination(client: Any) -> None:
    for _ in range(5):
        e = _seed_event(1)
        _seed_consolidated(1, winner_event_id=e)
    body1 = client.get("/calendar/events?limit=2").json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None
    body2 = client.get(f"/calendar/events?limit=2&cursor={body1['next_cursor']}").json()
    assert body2["items"][0]["id"] > body1["items"][-1]["id"]


def test_list_events_empty(client: Any) -> None:
    assert client.get("/calendar/events").json() == {"items": [], "next_cursor": None}


# ---- /calendar/dedup-candidates ------------------------------------------------------------------


def test_list_dedup_candidates_shape(client: Any) -> None:
    a = _seed_event(1, title="Cena de fin de año", origin="extraction", source_inbox_ids=[7, 8])
    b = _seed_event(1, title="Cena fin de año 🎉", origin="provider", provider="google")
    lo, hi = sorted((a, b))
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO mod_calendar_dedup_candidates
                  (user_id, event_a_id, event_b_id, reason, score, status)
                VALUES (1, :a, :b, 'titulo+fecha similares', 0.86, 'candidate')
                """
            ),
            {"a": lo, "b": hi},
        )
    body = client.get("/calendar/dedup-candidates").json()
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert row["status"] == "candidate"
    assert row["score"] == 0.86 and isinstance(row["score"], float)
    assert row["decided_by"] is None and row["confidence"] is None
    assert row["a"]["id"] == lo and row["b"]["id"] == hi
    assert {row["a"]["origin"], row["b"]["origin"]} == {"extraction", "provider"}
    # source_inbox_ids cruza para enlazar al mensaje de origen; el provider no tiene (vacío)
    assert row["a"]["source_inbox_ids"] == [7, 8]
    assert row["b"]["source_inbox_ids"] == []


def test_list_dedup_filter_by_status(client: Any) -> None:
    a, b = _seed_event(1, title="x"), _seed_event(1, title="y")
    c, d = _seed_event(1, title="z"), _seed_event(1, title="w")
    with connection() as conn:
        conn.execute(
            text(
                "INSERT INTO mod_calendar_dedup_candidates "
                "(user_id, event_a_id, event_b_id, reason, status) VALUES "
                "(1, :a, :b, 'r', 'candidate'), (1, :c, :d, 'r', 'confirmed')"
            ),
            {"a": min(a, b), "b": max(a, b), "c": min(c, d), "d": max(c, d)},
        )
    assert len(client.get("/calendar/dedup-candidates?status=confirmed").json()["items"]) == 1
    assert len(client.get("/calendar/dedup-candidates").json()["items"]) == 2


# ---- /calendar/conflicts -------------------------------------------------------------------------


def test_list_conflicts_shape(client: Any) -> None:
    ea = _seed_event(1, protected=True, priority_rank=100)
    eb = _seed_event(1, protected=True, priority_rank=60)
    ca = _seed_consolidated(1, winner_event_id=ea, title="Vuelo BOG → MEX")
    cb = _seed_consolidated(1, winner_event_id=eb, title="Dentista")
    lo, hi = sorted((ca, cb))
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO mod_calendar_conflicts
                  (user_id, consolidated_a_id, consolidated_b_id, reason, status)
                VALUES (1, :a, :b, 'time_overlap_high_priority', 'pending')
                """
            ),
            {"a": lo, "b": hi},
        )
    body = client.get("/calendar/conflicts").json()
    assert len(body["items"]) == 1
    row = body["items"][0]
    assert row["status"] == "pending"
    assert row["a"]["id"] == lo and row["b"]["id"] == hi
    # la prioridad/protección viene del ganador de cada consolidado
    ranks = {row["a"]["priority_rank"], row["b"]["priority_rank"]}
    assert ranks == {100, 60}
    assert row["a"]["protected"] is True


def test_list_conflicts_filter_by_status(client: Any) -> None:
    ea, eb = _seed_event(1), _seed_event(1)
    ca = _seed_consolidated(1, winner_event_id=ea)
    cb = _seed_consolidated(1, winner_event_id=eb)
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_calendar_conflicts "
                "(user_id, consolidated_a_id, consolidated_b_id, reason, status) "
                "VALUES (1, :a, :b, 'r', 'resolved')"
            ),
            {"a": min(ca, cb), "b": max(ca, cb)},
        )
    assert client.get("/calendar/conflicts?status=pending").json()["items"] == []
    assert len(client.get("/calendar/conflicts?status=resolved").json()["items"]) == 1


def _seed_conflict(a_cons: int, b_cons: int, *, status: str = "pending") -> None:
    lo, hi = sorted((a_cons, b_cons))
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_calendar_conflicts "
                "(user_id, consolidated_a_id, consolidated_b_id, reason, status) "
                "VALUES (1, :a, :b, 'time_overlap_high_priority', :st)"
            ),
            {"a": lo, "b": hi, "st": status},
        )


def test_list_conflicts_groups_recurring_series(client: Any) -> None:
    # Dos series recurrentes que chocan en 2 instancias → UN solo item con instance_count=2.
    # Títulos DISTINTOS por instancia: lo que agrupa es la serie de los miembros, no el fallback.
    d1, d2 = date(2026, 7, 1), date(2026, 8, 1)
    for d in (d1, d2):
        a = _seed_event(1, priority_rank=100, starts_on=d, recurring_event_id="serA")
        b = _seed_event(1, priority_rank=100, starts_on=d, recurring_event_id="serB")
        ca = _seed_consolidated(1, winner_event_id=a, title=f"Clase {d}", starts_on=d)
        cb = _seed_consolidated(1, winner_event_id=b, title=f"Turno {d}", starts_on=d)
        _link(1, ca, a)
        _link(1, cb, b)
        _seed_conflict(ca, cb)

    items = client.get("/calendar/conflicts").json()["items"]
    assert len(items) == 1
    it = items[0]
    assert it["instance_count"] == 2
    assert it["recurring"] is True
    assert it["first_on"] == "2026-07-01"
    assert it["last_on"] == "2026-08-01"


def test_list_conflicts_series_from_any_member_winner_gone(client: Any) -> None:
    # El ganador puede estar borrado (winner_event_id NULL, el caso del incidente): la serie
    # sale de CUALQUIER miembro linkeado, no del ganador.
    for d in (date(2026, 7, 1), date(2026, 8, 1)):
        a = _seed_event(1, starts_on=d, recurring_event_id="serA")
        b = _seed_event(1, starts_on=d, recurring_event_id="serB")
        ca = _seed_consolidated(1, winner_event_id=None, title=f"Clase {d}", starts_on=d)
        cb = _seed_consolidated(1, winner_event_id=None, title=f"Turno {d}", starts_on=d)
        _link(1, ca, a)
        _link(1, cb, b)
        _seed_conflict(ca, cb)

    items = client.get("/calendar/conflicts").json()["items"]
    assert len(items) == 1
    assert items[0]["instance_count"] == 2


def test_list_conflicts_fallback_title_time_groups(client: Any) -> None:
    # Sin serie en ningún miembro (extracciones de correo): agrupa por título normalizado + hora
    # (el case/espaciado varía entre instancias y normalize los colapsa igual).
    titles_a = ("Cortar  CABELLO", "cortar cabello")
    titles_b = ("Electrón 4700", "ELECTRÓN  4700")
    for i, d in enumerate((date(2026, 7, 2), date(2026, 8, 6))):
        a = _seed_event(1, starts_on=d)
        b = _seed_event(1, starts_on=d)
        ca = _seed_consolidated(1, winner_event_id=a, title=titles_a[i], starts_on=d)
        cb = _seed_consolidated(1, winner_event_id=b, title=titles_b[i], starts_on=d)
        _link(1, ca, a)
        _link(1, cb, b)
        _seed_conflict(ca, cb)

    items = client.get("/calendar/conflicts").json()["items"]
    assert len(items) == 1
    assert items[0]["instance_count"] == 2


def test_list_conflicts_distinct_titles_not_grouped(client: Any) -> None:
    # Mismo horario pero títulos distintos → cada choque es su propio item (no agrupa).
    for i, d in enumerate((date(2026, 7, 2), date(2026, 8, 6))):
        a = _seed_event(1, starts_on=d)
        b = _seed_event(1, starts_on=d)
        ca = _seed_consolidated(1, winner_event_id=a, title=f"Evento A{i}", starts_on=d)
        cb = _seed_consolidated(1, winner_event_id=b, title=f"Evento B{i}", starts_on=d)
        _link(1, ca, a)
        _link(1, cb, b)
        _seed_conflict(ca, cb)

    items = client.get("/calendar/conflicts").json()["items"]
    assert len(items) == 2
    assert all(it["instance_count"] == 1 for it in items)


def test_list_conflicts_oneoff_not_grouped(client: Any) -> None:
    # Serie recurrente vs evento único: choca 1 vez → item suelto (instance_count=1).
    rec = _seed_event(1, priority_rank=100, recurring_event_id="serA")
    one = _seed_event(1, priority_rank=100)
    _seed_conflict(
        _seed_consolidated(1, winner_event_id=rec),
        _seed_consolidated(1, winner_event_id=one),
    )
    items = client.get("/calendar/conflicts").json()["items"]
    assert len(items) == 1
    assert items[0]["instance_count"] == 1
    assert items[0]["recurring"] is False


# ---- /calendar/sync-runs -------------------------------------------------------------------------


def test_list_sync_runs_account_label(client: Any) -> None:
    acc = _seed_provider_account(1, provider="google", account_label="Personal")
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO mod_calendar_sync_runs
                  (user_id, provider_account_id, direction, pulled, created, modified, deleted,
                   unchanged, dedup_pairs, errors, status)
                VALUES (1, :acc, 'ingress', 568, 12, 4, 1, 551, 3, 0, 'ok')
                """
            ),
            {"acc": acc},
        )
    body = client.get("/calendar/sync-runs").json()
    assert len(body["items"]) == 1
    run = body["items"][0]
    assert run["account"] == "google · Personal"
    assert run["direction"] == "ingress"
    assert run["pulled"] == 568 and run["dedup_pairs"] == 3


# ---- /calendar/provider-accounts -----------------------------------------------------------------


def test_list_provider_accounts_never_leaks_token(client: Any) -> None:
    _seed_provider_account(
        1, token_path_env="GOOGLE_CALENDAR_TOKEN_PATH", sync_token="CAES-secreto-opaco"
    )
    body = client.get("/calendar/provider-accounts").json()
    assert len(body["items"]) == 1
    acc = body["items"][0]
    assert acc["provider"] == "google"
    assert acc["token_path_env"] == "GOOGLE_CALENDAR_TOKEN_PATH"  # NOMBRE de env var, no el token
    assert acc["sync_token_present"] is True
    assert acc["write_back"] is True
    # el cursor/token nunca cruza el wire
    assert "sync_token" not in acc
    assert "CAES-secreto-opaco" not in str(body)


def test_list_provider_accounts_cross_tenant_scoped(client: Any, seed_user2: int) -> None:
    _seed_provider_account(1, account_label="mío")
    _seed_provider_account(seed_user2, account_label="ajeno")
    items = client.get("/calendar/provider-accounts").json()["items"]
    assert len(items) == 1 and items[0]["account_label"] == "mío"


# ---- /calendar/sync-health + /calendar/accounts/{id}/sync ----------------------------------------


def test_sync_health_shape_without_secrets(client: Any) -> None:
    acc = _seed_provider_account(1, sync_token="CAES-secreto-opaco")
    body = client.get("/calendar/sync-health").json()
    assert body["overall"] == "nunca"  # cuenta sin corridas todavía
    assert body["auto_sync_active"] is False
    a = body["accounts"][0]
    assert a["account_id"] == acc
    assert a["cursor_state"] == "incremental"
    assert a["last_pull_at"] is None
    assert "CAES-secreto-opaco" not in str(body)  # el cursor jamás cruza el wire


def test_sync_now_unknown_account_404(client: Any, seed_user2: int) -> None:
    ajena = _seed_provider_account(seed_user2, account_label="ajena")
    assert client.post("/calendar/accounts/99999/sync").status_code == 404
    assert client.post(f"/calendar/accounts/{ajena}/sync").status_code == 404  # de otro user


def test_sync_now_pulls_and_consolidates(client: Any, monkeypatch: Any) -> None:
    from memex.modules.calendar.sync import SyncStats

    acc = _seed_provider_account(1)

    async def fake_run_pull(user_id: int, account_id: int, **kw: Any) -> SyncStats:
        assert (user_id, account_id) == (1, acc)
        return SyncStats(pulled=2, created=2)

    monkeypatch.setattr("memex.api.routers.calendar.run_pull", fake_run_pull)
    resp = client.post(f"/calendar/accounts/{acc}/sync")
    assert resp.status_code == 200
    data = resp.json()
    assert data["created"] == 2
    assert data["status"] == "ok"
    assert "orphans" in data  # la consolidación corrió después del pull


def test_list_events_exposes_resolved_place(client: Any) -> None:
    e = _seed_event(1)
    cid = _seed_consolidated(1, winner_event_id=e, title="Con lugar")
    _link(1, cid, e)
    with connection() as c:
        pid = int(
            c.execute(
                text(
                    "INSERT INTO geo_places (user_id, name, formatted_address, lat, lng) "
                    "VALUES (1, 'Aula 301', 'Cra 7 #40-62', 4.6286, -74.065) RETURNING id"
                )
            ).scalar_one()
        )
        c.execute(
            text("UPDATE mod_calendar_consolidated SET place_id = :p WHERE id = :i"),
            {"p": pid, "i": cid},
        )

    item = client.get("/calendar/events").json()["items"][0]
    assert item["place_name"] == "Aula 301"
    assert item["place_address"] == "Cra 7 #40-62"

    # sin FK → nullables
    cid2 = _seed_consolidated(1, winner_event_id=None, title="Sin lugar")
    _link(1, cid2, _seed_event(1))
    items = client.get("/calendar/events").json()["items"]
    sin = next(i for i in items if i["title"] == "Sin lugar")
    assert sin["place_name"] is None and sin["place_address"] is None


def test_calendar_settings_default_and_patch(client: Any) -> None:
    assert client.get("/calendar/settings").json() == {"llm_on_past_events": False}
    resp = client.patch("/calendar/settings", json={"llm_on_past_events": True})
    assert resp.status_code == 200
    assert resp.json() == {"llm_on_past_events": True}
    assert client.get("/calendar/settings").json() == {"llm_on_past_events": True}


def test_sync_now_provider_error_is_502(client: Any, monkeypatch: Any) -> None:
    from memex.modules.calendar.providers.base import CalendarProviderError

    acc = _seed_provider_account(1)

    async def boom(user_id: int, account_id: int, **kw: Any) -> Any:
        raise CalendarProviderError(401, "client error 401")

    monkeypatch.setattr("memex.api.routers.calendar.run_pull", boom)
    resp = client.post(f"/calendar/accounts/{acc}/sync")
    assert resp.status_code == 502
    assert "No se pudo sincronizar" in resp.json()["detail"]
