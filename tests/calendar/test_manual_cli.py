"""CRUD manual de eventos (`memex calendario …`): series puras, add/list/show/update/rm.

Los comandos se ejercitan vía `main(argv)` del CLI del módulo (mismo binario que forwardea el
agente); los asserts de salida van sobre la línea JSON (`--json` = ÚLTIMA línea de stdout), no
sobre los acentos del texto humano (cp1252 en Windows los degrada).
"""

from __future__ import annotations

import json
from datetime import date, time
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.calendar.cli import main as cal_main
from memex.modules.calendar.consolidate import run_consolidation
from memex.modules.calendar.manual import (
    MAX_SERIES_INSTANCES,
    ManualEventError,
    series_instances,
)

# ----- puro: series_instances ------------------------------------------------------ #


def test_series_daily() -> None:
    out = series_instances(date(2026, 6, 1), "daily", date(2026, 6, 4))
    assert out == [date(2026, 6, d) for d in (1, 2, 3, 4)]


def test_series_weekly() -> None:
    out = series_instances(date(2026, 6, 1), "weekly", date(2026, 6, 20))
    assert out == [date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15)]


def test_series_monthly_skips_short_months() -> None:
    # Día 31: los meses sin 31 (sep/nov…) se SALTAN, no se corren al último día.
    out = series_instances(date(2026, 8, 31), "monthly", date(2026, 12, 31))
    assert out == [date(2026, 8, 31), date(2026, 10, 31), date(2026, 12, 31)]


def test_series_until_before_start_rejected() -> None:
    with pytest.raises(ManualEventError):
        series_instances(date(2026, 6, 10), "weekly", date(2026, 6, 1))


def test_series_cap() -> None:
    with pytest.raises(ManualEventError, match="máximo"):
        series_instances(date(2026, 1, 1), "daily", date(2028, 1, 1))
    # justo en el tope no revienta
    out = series_instances(date(2026, 1, 1), "daily", date(2026, 1, 1))
    assert len(out) <= MAX_SERIES_INSTANCES


# ----- helpers DB ------------------------------------------------------------------- #


def _last_json(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _seed_raw(
    title: str,
    *,
    starts_on: date = date(2026, 6, 20),
    start_time: time | None = time(9, 0),
    origin: str = "extraction",
    inbox_ids: list[int] | None = None,
    recurring: str | None = None,
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_calendar_events
                      (user_id, source_inbox_ids, title, starts_on, start_time, origin,
                       recurring_event_id)
                    VALUES (1, CAST(:ids AS BIGINT[]), :t, :d, :st, :o, :rec)
                    RETURNING id
                    """
                ),
                {
                    "ids": inbox_ids or [],
                    "t": title,
                    "d": starts_on,
                    "st": start_time,
                    "o": origin,
                    "rec": recurring,
                },
            ).scalar_one()
        )


def _cons_row(cons_id: int) -> dict[str, Any]:
    with connection() as c:
        return dict(
            c.execute(
                text(
                    "SELECT title, starts_on, start_time, location, deleted, deleted_source, "
                    "winner_event_id FROM mod_calendar_consolidated WHERE id = :i"
                ),
                {"i": cons_id},
            )
            .mappings()
            .one()
        )


def _event_row(event_id: int) -> dict[str, Any]:
    with connection() as c:
        return dict(
            c.execute(
                text(
                    "SELECT title, origin, manual, priority_rank, recurring_event_id "
                    "FROM mod_calendar_events WHERE id = :i"
                ),
                {"i": event_id},
            )
            .mappings()
            .one()
        )


# ----- add --------------------------------------------------------------------------- #


def test_add_single_event(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cal_main(
        [
            "add",
            "--title",
            "Dentista",
            "--date",
            "2026-06-20",
            "--time",
            "09:00",
            "--end-time",
            "10:00",
            "--location",
            "Consultorio",
            "--json",
        ]
    )
    assert rc == 0
    result = _last_json(capsys)
    assert result["instances"] == 1
    assert result["series_id"] is None
    event = _event_row(result["event_ids"][0])
    assert (event["origin"], event["manual"], event["priority_rank"]) == ("manual", True, 100)
    cons = _cons_row(result["consolidated_ids"][0])
    assert cons["title"] == "Dentista"
    assert cons["start_time"] == time(9, 0)
    assert cons["winner_event_id"] == result["event_ids"][0]


def test_add_series_shares_local_series_id(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cal_main(
        [
            "add",
            "--title",
            "Yoga",
            "--date",
            "2026-06-01",
            "--time",
            "07:00",
            "--every",
            "weekly",
            "--until",
            "2026-06-15",
            "--json",
        ]
    )
    assert rc == 0
    result = _last_json(capsys)
    assert result["instances"] == 3
    assert str(result["series_id"]).startswith("memex:")
    assert len(result["consolidated_ids"]) == 3  # cada instancia es su propio consolidado
    series = {_event_row(e)["recurring_event_id"] for e in result["event_ids"]}
    assert series == {result["series_id"]}


def test_add_every_without_until_fails(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cal_main(["add", "--title", "X", "--date", "2026-06-01", "--every", "weekly"])
    assert rc == 1
    assert "--until" in capsys.readouterr().err


def test_add_duplicate_of_existing_marks_candidate_pair(
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = _seed_raw("Dentista", origin="provider")
    run_consolidation(1)
    rc = cal_main(
        ["add", "--title", "Dentista", "--date", "2026-06-20", "--time", "09:00", "--json"]
    )
    assert rc == 0
    result = _last_json(capsys)
    assert result["dedup_pairs"] >= 1  # F1 lo marcó como posible duplicado (queda candidate)
    with connection() as c:
        status = c.execute(
            text(
                "SELECT status FROM mod_calendar_dedup_candidates "
                "WHERE :a IN (event_a_id, event_b_id) AND :b IN (event_a_id, event_b_id)"
            ),
            {"a": existing, "b": result["event_ids"][0]},
        ).scalar_one()
    assert status == "candidate"


# ----- list / show -------------------------------------------------------------------- #


def test_list_chronological_and_windowed(capsys: pytest.CaptureFixture[str]) -> None:
    cal_main(["add", "--title", "Tarde", "--date", "2026-07-02", "--time", "18:00"])
    cal_main(["add", "--title", "Lejos", "--date", "2026-09-01"])
    cal_main(["add", "--title", "Cerca", "--date", "2026-07-01", "--time", "08:00"])
    capsys.readouterr()

    rc = cal_main(["list", "--since", "2026-07-01", "--until", "2026-07-31", "--json"])
    assert rc == 0
    result = _last_json(capsys)
    assert [i["title"] for i in result["items"]] == ["Cerca", "Tarde"]  # cronológico, acotado


def test_show_detail_members_and_conflicts(capsys: pytest.CaptureFixture[str]) -> None:
    cal_main(
        [
            "add",
            "--title",
            "Clase",
            "--date",
            "2026-06-20",
            "--time",
            "09:00",
            "--end-time",
            "11:00",
            "--protected",
            "--json",
        ]
    )
    first = _last_json(capsys)
    cal_main(
        [
            "add",
            "--title",
            "Turno médico",
            "--date",
            "2026-06-20",
            "--time",
            "10:00",
            "--protected",
            "--json",
        ]
    )
    second = _last_json(capsys)

    rc = cal_main(["show", str(first["consolidated_ids"][0]), "--json"])
    assert rc == 0
    detail = _last_json(capsys)
    assert detail["title"] == "Clase"
    assert len(detail["members"]) == 1
    assert detail["members"][0]["is_winner"] is True
    # ambos protegidos y solapados → conflicto pendiente visible desde el detalle
    assert [c["with_id"] for c in detail["pending_conflicts"]] == [second["consolidated_ids"][0]]


def test_show_missing_event_fails(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cal_main(["show", "99999"])
    assert rc == 1
    assert "No existe" in capsys.readouterr().err


def test_show_includes_resolved_place(capsys: pytest.CaptureFixture[str]) -> None:
    cal_main(
        ["add", "--title", "Cita", "--date", "2026-06-20", "--location", "Consultorio", "--json"]
    )
    cons_id = _last_json(capsys)["consolidated_ids"][0]

    cal_main(["show", str(cons_id), "--json"])
    assert _last_json(capsys)["place"] is None  # sin geocoding todavía

    with connection() as c:  # simula la resolución del catálogo (el geocoding real es otro test)
        pid = int(
            c.execute(
                text(
                    "INSERT INTO geo_places (user_id, name, formatted_address, lat, lng) "
                    "VALUES (1, 'Consultorio', 'Cra 7 #45-23, Bogotá', 4.6286, -74.065) "
                    "RETURNING id"
                )
            ).scalar_one()
        )
        c.execute(
            text("UPDATE mod_calendar_consolidated SET place_id = :p WHERE id = :i"),
            {"p": pid, "i": cons_id},
        )

    cal_main(["show", str(cons_id), "--json"])
    place = _last_json(capsys)["place"]
    assert place["id"] == pid
    assert place["name"] == "Consultorio"
    assert place["formatted_address"] == "Cra 7 #45-23, Bogotá"


# ----- update ------------------------------------------------------------------------- #


def test_update_in_place_reflects_immediately(capsys: pytest.CaptureFixture[str]) -> None:
    cal_main(["add", "--title", "Dentista", "--date", "2026-06-20", "--time", "09:00", "--json"])
    cons_id = _last_json(capsys)["consolidated_ids"][0]

    rc = cal_main(["update", str(cons_id), "--title", "Odontología", "--time", "10:30", "--json"])
    assert rc == 0
    result = _last_json(capsys)
    assert result["updated"][0]["mode"] == "in_place"
    cons = _cons_row(cons_id)
    # el doble-write (fila cruda + consolidado) deja la capa consolidada al día sin esperar
    # un cambio de membresía
    assert cons["title"] == "Odontología"
    assert cons["start_time"] == time(10, 30)


def test_update_override_wins_over_provider(capsys: pytest.CaptureFixture[str]) -> None:
    raw = _seed_raw("Clase de cálculo", origin="provider", start_time=time(7, 0))
    run_consolidation(1)
    with connection() as c:
        cons_id = int(
            c.execute(
                text("SELECT consolidated_id FROM mod_calendar_event_links WHERE event_id = :e"),
                {"e": raw},
            ).scalar_one()
        )

    rc = cal_main(["update", str(cons_id), "--location", "Aula 305", "--json"])
    assert rc == 0
    result = _last_json(capsys)
    assert result["updated"][0]["mode"] == "manual_override"
    new_event = result["updated"][0]["event_id"]

    cons = _cons_row(cons_id)
    assert cons["location"] == "Aula 305"
    assert cons["winner_event_id"] == new_event  # el manual ganó la consolidación
    with connection() as c:
        decided = c.execute(
            text(
                "SELECT status, decided_by FROM mod_calendar_dedup_candidates "
                "WHERE :a IN (event_a_id, event_b_id) AND :b IN (event_a_id, event_b_id)"
            ),
            {"a": raw, "b": new_event},
        ).first()
    assert decided is not None
    assert (decided[0], decided[1]) == ("confirmed", "manual")


def test_update_series_applies_to_all_instances(capsys: pytest.CaptureFixture[str]) -> None:
    cal_main(
        [
            "add",
            "--title",
            "Yoga",
            "--date",
            "2026-06-01",
            "--time",
            "07:00",
            "--every",
            "weekly",
            "--until",
            "2026-06-15",
            "--json",
        ]
    )
    cons_ids = _last_json(capsys)["consolidated_ids"]

    rc = cal_main(["update", str(cons_ids[1]), "--series", "--time", "08:00", "--json"])
    assert rc == 0
    assert _last_json(capsys)["instances"] == 3
    for cid in cons_ids:
        assert _cons_row(cid)["start_time"] == time(8, 0)

    # la fecha es por-instancia: no se combina con --series (error de uso)
    assert cal_main(["update", str(cons_ids[0]), "--series", "--date", "2026-06-02"]) == 2


def test_update_without_changes_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    cal_main(["add", "--title", "X", "--date", "2026-06-20", "--json"])
    cons_id = _last_json(capsys)["consolidated_ids"][0]
    assert cal_main(["update", str(cons_id)]) == 2


# ----- rm ----------------------------------------------------------------------------- #


def test_rm_tombstones_user_and_resolves_conflicts(capsys: pytest.CaptureFixture[str]) -> None:
    cal_main(
        [
            "add",
            "--title",
            "Clase",
            "--date",
            "2026-06-20",
            "--time",
            "09:00",
            "--end-time",
            "11:00",
            "--protected",
            "--json",
        ]
    )
    a = _last_json(capsys)["consolidated_ids"][0]
    cal_main(
        [
            "add",
            "--title",
            "Turno",
            "--date",
            "2026-06-20",
            "--time",
            "10:00",
            "--protected",
            "--json",
        ]
    )
    _last_json(capsys)
    with connection() as c:
        pending = c.execute(
            text("SELECT count(*) FROM mod_calendar_conflicts WHERE status = 'pending'")
        ).scalar_one()
    assert pending == 1

    rc = cal_main(["rm", str(a), "--json"])
    assert rc == 0
    cons = _cons_row(a)
    assert (cons["deleted"], cons["deleted_source"]) == (True, "user")
    with connection() as c:
        pending = c.execute(
            text("SELECT count(*) FROM mod_calendar_conflicts WHERE status = 'pending'")
        ).scalar_one()
    assert pending == 0  # borrar uno de los dos resuelve el choque

    # y NO aparece más en list
    cal_main(["list", "--since", "2026-06-19", "--json"])
    titles = [i["title"] for i in _last_json(capsys)["items"]]
    assert "Clase" not in titles


def test_rm_series_removes_all_instances(capsys: pytest.CaptureFixture[str]) -> None:
    cal_main(
        [
            "add",
            "--title",
            "Yoga",
            "--date",
            "2026-06-01",
            "--every",
            "daily",
            "--until",
            "2026-06-03",
            "--json",
        ]
    )
    cons_ids = _last_json(capsys)["consolidated_ids"]

    rc = cal_main(["rm", str(cons_ids[0]), "--series", "--json"])
    assert rc == 0
    assert _last_json(capsys)["instances"] == 3
    for cid in cons_ids:
        assert _cons_row(cid)["deleted"] is True

    rc = cal_main(["rm", str(cons_ids[0])])  # ya borrado → error claro
    assert rc == 1
