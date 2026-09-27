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
            "--fijado",
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
            "--fijado",
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
            "--fijado",
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
            "--fijado",
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

    rc = cal_main(["rm", str(a)])  # ya borrado → error claro
    assert rc == 1


def test_list_includes_multiday_event_in_progress(capsys: pytest.CaptureFixture[str]) -> None:
    """«¿Qué tengo mañana?» en pleno evento de varios días: el evento que empezó antes de la
    ventana pero sigue vigente entra (solapa), y el que ya terminó no."""
    cal_main(["add", "--title", "Curso", "--date", "2026-09-14", "--end-date", "2026-09-18"])
    cal_main(["add", "--title", "Pasado", "--date", "2026-09-10", "--end-date", "2026-09-12"])
    cal_main(["add", "--title", "Puntual", "--date", "2026-09-16", "--time", "10:00"])
    capsys.readouterr()

    rc = cal_main(["list", "--since", "2026-09-16", "--until", "2026-09-16", "--json"])
    assert rc == 0
    result = _last_json(capsys)
    assert [i["title"] for i in result["items"]] == ["Curso", "Puntual"]


def test_recheck_registers_missing_candidates_with_current_gate(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`recheck` corre la FASE 1 sobre todo el historico con la compuerta vigente y registra los
    pares que faltaban; los ya registrados no se duplican (idempotente)."""
    cal_main(
        [
            "add",
            "--title",
            "Inicio de clases intensivo de ingles",
            "--date",
            "2026-09-02",
            "--time",
            "15:10",
            "--location",
            "Centro Britano Americano, Calle 19",
        ]
    )
    cal_main(
        [
            "add",
            "--title",
            "Inicio de clases de ingles",
            "--date",
            "2026-09-02",
            "--time",
            "15:10",
            "--location",
            "Sede Centro, Calle 19, salon 314",
        ]
    )
    with connection() as c:
        c.execute(
            text("DELETE FROM mod_calendar_dedup_candidates")
        )  # como si la compuerta vieja no los hubiera marcado
    capsys.readouterr()
    assert cal_main(["recheck"]) == 0
    out = capsys.readouterr().out
    assert "candidatos_nuevos=1" in out
    with connection() as c:
        rows = c.execute(text("SELECT reason, status FROM mod_calendar_dedup_candidates")).all()
    assert [(r[0], r[1]) for r in rows] == [("same-start+weak-text", "candidate")]
    assert cal_main(["recheck"]) == 0
    assert "candidatos_nuevos=0" in capsys.readouterr().out


# ----- recurrencia (maestro + regla, estilo Google) ----------------------------------- #


def test_add_recurring_creates_one_master_and_expands_on_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Una serie es UNA fila (maestro con RRULE); `list` la expande a sus ocurrencias dentro de la
    ventana. Las ocurrencias no se materializan."""
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
    assert result["instances"] == 1 and len(result["consolidated_ids"]) == 1
    assert result["recurrence"] == ["RRULE:FREQ=WEEKLY;UNTIL=20260615"]
    assert result["recurrence_human"] == "semanal hasta 2026-06-15"
    assert str(result["series_id"]).startswith("memex:")
    assert _event_row(result["event_ids"][0])["recurring_event_id"] == result["series_id"]
    with connection() as c:
        assert c.execute(text("SELECT count(*) FROM mod_calendar_events")).scalar() == 1

    cal_main(["list", "--since", "2026-06-01", "--until", "2026-06-30", "--json"])
    items = _last_json(capsys)["items"]
    assert [i["starts_on"] for i in items] == ["2026-06-01", "2026-06-08", "2026-06-15"]
    assert all(i["id"] == result["consolidated_ids"][0] and i["recurring"] for i in items)
    assert items[0]["start_time"] == "07:00" and items[0]["series_id"] == result["series_id"]


def test_add_recurring_without_end_is_unbounded(capsys: pytest.CaptureFixture[str]) -> None:
    """«Todos los jueves» sin fin: la regla no tiene UNTIL ni COUNT y la expansión de cualquier
    ventana futura la trae igual (una regla, no mil filas)."""
    rc = cal_main(
        [
            "add",
            "--title",
            "Terapia",
            "--date",
            "2026-06-04",
            "--time",
            "18:00",
            "--every",
            "weekly",
            "--byday",
            "jueves",
            "--json",
        ]
    )
    assert rc == 0
    result = _last_json(capsys)
    assert result["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TH"]
    cal_main(["list", "--since", "2027-03-01", "--until", "2027-03-31", "--json"])
    days = [i["starts_on"] for i in _last_json(capsys)["items"]]
    assert days == ["2027-03-04", "2027-03-11", "2027-03-18", "2027-03-25"]
    # --until/--byday/--count sin --every es error de uso
    assert cal_main(["add", "--title", "X", "--date", "2026-06-01", "--byday", "MO"]) == 1


def test_update_master_changes_whole_series_and_instance_creates_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
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
            "daily",
            "--until",
            "2026-06-03",
            "--json",
        ]
    )
    cons_id = _last_json(capsys)["consolidated_ids"][0]

    # editar el maestro = toda la serie (una sola fila)
    assert cal_main(["update", str(cons_id), "--time", "08:00", "--json"]) == 0
    assert _cons_row(cons_id)["start_time"] == time(8, 0)
    cal_main(["list", "--since", "2026-06-01", "--until", "2026-06-03", "--json"])
    assert {i["start_time"] for i in _last_json(capsys)["items"]} == {"08:00"}

    # una ocurrencia distinta: excepción (fila propia que apunta a la serie)
    rc = cal_main(
        [
            "update",
            str(cons_id),
            "--instance",
            "2026-06-02",
            "--time",
            "10:00",
            "--title",
            "Yoga (cambio de hora)",
            "--json",
        ]
    )
    assert rc == 0
    exc = _last_json(capsys)
    assert exc["mode"] == "exception_created" and exc["instance_on"] == "2026-06-02"
    cal_main(["list", "--since", "2026-06-01", "--until", "2026-06-03", "--json"])
    items = _last_json(capsys)["items"]
    assert [(i["starts_on"], i["start_time"], i["title"]) for i in items] == [
        ("2026-06-01", "08:00", "Yoga"),
        ("2026-06-02", "10:00", "Yoga (cambio de hora)"),
        ("2026-06-03", "08:00", "Yoga"),
    ]
    assert items[1]["is_exception"] is True and items[1]["series_id"] == items[0]["series_id"]
    # fecha fuera de la regla → error
    assert cal_main(["update", str(cons_id), "--instance", "2026-06-09", "--time", "11:00"]) == 1


def test_rm_instance_adds_exdate_and_rm_master_removes_series(
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    cons_id = _last_json(capsys)["consolidated_ids"][0]
    assert cal_main(["rm", str(cons_id), "--instance", "2026-06-02", "--json"]) == 0
    out = _last_json(capsys)
    assert out["recurrence"] == ["RRULE:FREQ=DAILY;UNTIL=20260603", "EXDATE;VALUE=DATE:20260602"]
    cal_main(["list", "--since", "2026-06-01", "--until", "2026-06-03", "--json"])
    assert [i["starts_on"] for i in _last_json(capsys)["items"]] == ["2026-06-01", "2026-06-03"]
    cal_main(["show", str(cons_id), "--json"])
    detail = _last_json(capsys)
    assert detail["recurrence_human"] == "diaria hasta 2026-06-03 (1 excepción)"

    assert cal_main(["rm", str(cons_id), "--json"]) == 0
    assert _cons_row(cons_id)["deleted"] is True
    cal_main(["list", "--since", "2026-06-01", "--until", "2026-06-30", "--json"])
    assert _last_json(capsys)["items"] == []


def test_single_event_dedups_against_master_occurrence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Caso Britano: el programa es un maestro (lun-vie 15:10) y un correo trae «Inicio de clases»
    el 2 de septiembre a las 15:10: la FASE 1 proyecta el maestro a ese día y lo marca como par
    candidato; un sábado a la misma hora NO (la serie no tiene ocurrencia)."""
    cal_main(
        [
            "add",
            "--title",
            "Programa Intensivo B1",
            "--date",
            "2026-09-02",
            "--time",
            "15:10",
            "--every",
            "weekly",
            "--byday",
            "MO,TU,WE,TH,FR",
            "--until",
            "2026-09-25",
            "--location",
            "Sede Centro",
            "--json",
        ]
    )
    master = _last_json(capsys)
    cal_main(
        [
            "add",
            "--title",
            "Inicio de clases intensivo",
            "--date",
            "2026-09-02",
            "--time",
            "15:10",
            "--location",
            "Sede Centro, salón 314",
            "--json",
        ]
    )
    first = _last_json(capsys)
    assert first["dedup_pairs"] == 1
    cal_main(
        [
            "add",
            "--title",
            "Inicio de clases intensivo",
            "--date",
            "2026-09-05",
            "--time",
            "15:10",
            "--location",
            "Sede Centro, salón 314",
            "--json",
        ]
    )
    saturday = _last_json(capsys)
    assert saturday["dedup_pairs"] == 0
    with connection() as c:
        reason = c.execute(
            text(
                "SELECT reason FROM mod_calendar_dedup_candidates WHERE :a IN (event_a_id, "
                "event_b_id) AND :b IN (event_a_id, event_b_id)"
            ),
            {"a": master["event_ids"][0], "b": first["event_ids"][0]},
        ).scalar_one()
    assert reason in ("time+location", "same-start+weak-text", "time+title")


def test_update_following_splits_series_and_rm_following_ends_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """«Esta y las siguientes»: desde el 3 de junio la clase pasa a las 09:00 (serie nueva) y la
    vieja termina el 2; después, terminar la nueva desde el 5 deja solo el 3 y el 4."""
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
            "daily",
            "--until",
            "2026-06-07",
            "--json",
        ]
    )
    cons_id = _last_json(capsys)["consolidated_ids"][0]
    rc = cal_main(
        [
            "update",
            str(cons_id),
            "--instance",
            "2026-06-03",
            "--following",
            "--time",
            "09:00",
            "--json",
        ]
    )
    assert rc == 0
    out = _last_json(capsys)
    assert out["mode"] == "series_split"
    assert out["old_recurrence"] == ["RRULE:FREQ=DAILY;UNTIL=20260602"]
    assert out["new_recurrence"] == ["RRULE:FREQ=DAILY;UNTIL=20260607"]
    new_cons = out["new_consolidated_ids"][0]
    cal_main(["list", "--since", "2026-06-01", "--until", "2026-06-07", "--json"])
    items = _last_json(capsys)["items"]
    assert [(i["starts_on"], i["start_time"], i["id"]) for i in items] == [
        ("2026-06-01", "07:00", cons_id),
        ("2026-06-02", "07:00", cons_id),
        ("2026-06-03", "09:00", new_cons),
        ("2026-06-04", "09:00", new_cons),
        ("2026-06-05", "09:00", new_cons),
        ("2026-06-06", "09:00", new_cons),
        ("2026-06-07", "09:00", new_cons),
    ]
    assert cal_main(["rm", str(new_cons), "--instance", "2026-06-05", "--following", "--json"]) == 0
    assert _last_json(capsys)["mode"] == "series_ended"
    cal_main(["list", "--since", "2026-06-01", "--until", "2026-06-07", "--json"])
    assert [i["starts_on"] for i in _last_json(capsys)["items"]] == [
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
        "2026-06-04",
    ]
    # --following sin --instance es error de uso; desde la primera ocurrencia = toda la serie
    assert cal_main(["rm", str(new_cons), "--following"]) == 2
    assert cal_main(["rm", str(cons_id), "--instance", "2026-06-01", "--following", "--json"]) == 0
    assert _cons_row(cons_id)["deleted"] is True


# ----- protección ------------------------------------------------------------------- #


def test_protect_marks_members_and_enables_conflicts(capsys: pytest.CaptureFixture[str]) -> None:
    """Una clase extraída de un correo nace sin protección: no cuenta para los choques. Al
    protegerla (toda la serie), un turno protegido a la misma hora sí choca."""
    _seed_raw(
        "Clase de inglés", origin="extraction", starts_on=date(2026, 6, 22), start_time=time(15, 10)
    )
    run_consolidation(1)
    with connection() as c:
        cons_id = int(c.execute(text("SELECT id FROM mod_calendar_consolidated")).scalar_one())
    cal_main(
        ["add", "--title", "Odontología", "--date", "2026-06-22", "--time", "15:30", "--fijado"]
    )
    with connection() as c:
        assert c.execute(text("SELECT count(*) FROM mod_calendar_conflicts")).scalar() == 0
    assert cal_main(["fijar", str(cons_id), "--json"]) == 0
    out = _last_json(capsys)
    assert out["protected"] is True and out["members"] == 1
    with connection() as c:
        assert c.execute(text("SELECT count(*) FROM mod_calendar_conflicts")).scalar() == 1
        assert (
            c.execute(
                text("SELECT protected FROM mod_calendar_events WHERE title = 'Clase de inglés'")
            ).scalar_one()
            is True
        )
    cal_main(["show", str(cons_id), "--json"])
    assert _last_json(capsys)["members"][0]["protected"] is True
    assert cal_main(["fijar", str(cons_id), "--off", "--json"]) == 0
    assert _last_json(capsys)["protected"] is False


def test_protect_rule_applies_on_every_consolidation(capsys: pytest.CaptureFixture[str]) -> None:
    """«Mis clases del Britano son inamovibles»: la regla protege lo que ya existe y lo que llegue
    después (extracción o proveedor), sin tocar lo que no coincide."""
    assert cal_main(["fijar-regla", "add", "--match", "britano", "--json"]) == 0
    assert _last_json(capsys)["rules"] == ["britano"]
    _seed_raw(
        "Programa Intensivo B1 - Centro Britano Americano",
        origin="extraction",
        starts_on=date(2026, 6, 22),
        start_time=time(15, 10),
    )
    _seed_raw(
        "Reunión de equipo",
        origin="extraction",
        starts_on=date(2026, 6, 23),
        start_time=time(10, 0),
    )
    stats = run_consolidation(1)
    assert stats.protected_by_rules == 1
    with connection() as c:
        rows = {
            str(t): bool(pr)
            for t, pr in c.execute(text("SELECT title, protected FROM mod_calendar_events")).all()
        }
    assert rows == {
        "Programa Intensivo B1 - Centro Britano Americano": True,
        "Reunión de equipo": False,
    }
    # idempotente y con acentos: «COLÓMBO» también matchea; la regla no se duplica
    assert cal_main(["fijar-regla", "add", "--match", "Britano", "--json"]) == 0
    assert _last_json(capsys)["rules"] == ["britano"]
    assert run_consolidation(1).protected_by_rules == 0
    assert cal_main(["fijar-regla", "rm", "--match", "BRITANO", "--json"]) == 0
    assert _last_json(capsys)["rules"] == []
    assert cal_main(["fijar-regla", "list"]) == 0
    assert "Sin reglas" in capsys.readouterr().out
