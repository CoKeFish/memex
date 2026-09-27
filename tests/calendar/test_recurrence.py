"""Recurrencia pura (RFC 5545 al estilo Google): serialización, expansión por ventana, EXDATE,
excepciones, series sin fin, descripción humana."""

from __future__ import annotations

from datetime import date, time

import pytest

from memex.modules.calendar.recurrence import (
    RecurrenceError,
    RecurrenceSpec,
    describe,
    exdate_line,
    exdates,
    is_recurring,
    is_unbounded,
    last_occurrence,
    next_occurrences,
    normalize_day,
    normalize_freq,
    occurrences,
    occurs_on,
    to_rrule,
    until_local,
    validate_lines,
)


def test_to_rrule_weekdays_until() -> None:
    spec = RecurrenceSpec(
        freq="weekly", byday=("MO", "TU", "WE", "TH", "FR"), until=date(2026, 9, 25)
    )
    assert to_rrule(spec) == "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925"
    assert to_rrule(RecurrenceSpec(freq="daily", interval=2, count=3)) == (
        "RRULE:FREQ=DAILY;INTERVAL=2;COUNT=3"
    )
    assert to_rrule(RecurrenceSpec(freq="weekly", byday=("TH",))) == "RRULE:FREQ=WEEKLY;BYDAY=TH"


def test_spec_validation_and_aliases() -> None:
    assert (
        normalize_day("lunes") == "MO"
        and normalize_day("jue") == "TH"
        and normalize_day("SA") == "SA"
    )
    assert normalize_freq("semanal") == "weekly" and normalize_freq("DAILY") == "daily"
    with pytest.raises(RecurrenceError):
        normalize_day("funday")
    with pytest.raises(RecurrenceError):
        RecurrenceSpec(freq="weekly", until=date(2026, 1, 1), count=2)
    with pytest.raises(RecurrenceError):
        RecurrenceSpec(freq="weekly", byday=("XX",))
    with pytest.raises(RecurrenceError):
        validate_lines(["RRULE:FREQ=NOPE"])
    with pytest.raises(RecurrenceError):
        validate_lines(["hola"])
    assert validate_lines([" RRULE:FREQ=DAILY ", ""]) == ("RRULE:FREQ=DAILY",)


def test_occurrences_weekdays_in_window_colombo() -> None:
    """Clases de lunes a viernes del 2 al 25 de septiembre a las 15:10: septiembre 2026 arranca
    en martes → 18 clases; la ventana 14-18 trae las 5 de esa semana; el sábado 19 nada."""
    rule = ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925"]
    start = date(2026, 9, 2)
    all_days = occurrences(
        start, rule, window_start=start, window_end=date(2026, 9, 30), start_time=time(15, 10)
    )
    assert len(all_days) == 18 and all_days[0] == start and all_days[-1] == date(2026, 9, 25)
    week = occurrences(start, rule, window_start=date(2026, 9, 14), window_end=date(2026, 9, 18))
    assert week == [date(2026, 9, 14 + i) for i in range(5)]
    assert occurs_on(start, rule, date(2026, 9, 19)) is False  # sábado
    assert occurs_on(start, rule, date(2026, 9, 16), start_time=time(15, 10)) is True
    assert (
        occurs_on(start, rule, date(2026, 9, 25), start_time=time(15, 10)) is True
    )  # UNTIL incluido


def test_unbounded_rule_is_cheap_and_exdate_excluded() -> None:
    rule = ["RRULE:FREQ=WEEKLY;BYDAY=TH", exdate_line(date(2026, 10, 8))]
    assert is_unbounded(rule) and is_recurring(rule)
    assert exdates(rule) == {date(2026, 10, 8)}
    days = occurrences(
        date(2026, 9, 3), rule, window_start=date(2026, 10, 1), window_end=date(2026, 10, 31)
    )
    assert days == [date(2026, 10, 1), date(2026, 10, 15), date(2026, 10, 22), date(2026, 10, 29)]
    # una excepción (ocurrencia reemplazada) también se quita de la expansión
    assert occurrences(
        date(2026, 9, 3),
        rule,
        window_start=date(2026, 10, 1),
        window_end=date(2026, 10, 31),
        exclude=[date(2026, 10, 15)],
    ) == [date(2026, 10, 1), date(2026, 10, 22), date(2026, 10, 29)]
    nxt = next_occurrences(date(2026, 9, 3), rule, after=date(2026, 9, 9), limit=3)
    assert nxt == [date(2026, 9, 10), date(2026, 9, 17), date(2026, 9, 24)]


def test_non_recurring_and_empty_window() -> None:
    assert occurrences(
        date(2026, 9, 2), [], window_start=date(2026, 9, 1), window_end=date(2026, 9, 3)
    ) == [date(2026, 9, 2)]
    assert (
        occurrences(
            date(2026, 9, 2), [], window_start=date(2026, 9, 3), window_end=date(2026, 9, 9)
        )
        == []
    )
    assert (
        occurrences(
            date(2026, 9, 2),
            ["RRULE:FREQ=DAILY"],
            window_start=date(2026, 9, 9),
            window_end=date(2026, 9, 1),
        )
        == []
    )
    assert not is_recurring([]) and not is_recurring(["EXDATE;VALUE=DATE:20260101"])


def test_describe_human() -> None:
    assert describe(["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925"]) == (
        "semanal (lun a vie) hasta 2026-09-25"
    )
    assert describe(["RRULE:FREQ=WEEKLY;BYDAY=TH", "EXDATE;VALUE=DATE:20261008"]) == (
        "semanal (jue) (1 excepción)"
    )
    assert describe(["RRULE:FREQ=DAILY;INTERVAL=2;COUNT=3"]) == "diaria cada 2, 3 veces"
    assert describe([]) == ""


def test_split_rrule_this_and_following() -> None:
    from memex.modules.calendar.recurrence import split_rrule

    old, new = split_rrule(
        [
            "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925",
            exdate_line(date(2026, 9, 10)),
            exdate_line(date(2026, 9, 21)),
        ],
        date(2026, 9, 14),
    )
    assert old == (
        "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260913",
        "EXDATE;VALUE=DATE:20260910",
    )
    assert new == (
        "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925",
        "EXDATE;VALUE=DATE:20260921",
    )
    old2, new2 = split_rrule(["RRULE:FREQ=DAILY;COUNT=10"], date(2026, 6, 5))
    assert old2 == ("RRULE:FREQ=DAILY;UNTIL=20260604",) and new2 == ("RRULE:FREQ=DAILY",)
    with pytest.raises(RecurrenceError):
        split_rrule(["EXDATE;VALUE=DATE:20260101"], date(2026, 6, 5))


def test_last_occurrence_single_bounded_and_unbounded() -> None:
    assert last_occurrence(date(2026, 9, 2), []) == date(2026, 9, 2)
    # L-V hasta el 25/09: la última es el viernes 25.
    assert last_occurrence(
        date(2026, 9, 2),
        ["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925"],
        start_time=time(15, 10),
    ) == date(2026, 9, 25)
    assert last_occurrence(date(2026, 9, 7), ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=3"]) == date(
        2026, 9, 21
    )
    assert last_occurrence(date(2026, 9, 7), ["RRULE:FREQ=WEEKLY;BYDAY=MO"]) is None


def test_google_utc_until_is_read_as_local_day() -> None:
    """Google escribe `UNTIL=…T045959Z` (UTC). Con el DTSTART naive de memex, dateutil rechazaba la
    línea y el pull moría en el dedup; y el día mostrado era el UTC (26) en vez del local (25)."""
    rule = [
        "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260926T045959Z"
    ]  # 25/09 23:59:59 Bogotá
    days = occurrences(
        date(2026, 9, 21),
        rule,
        window_start=date(2026, 9, 21),
        window_end=date(2026, 10, 31),
        start_time=time(15, 10),
    )
    assert days == [date(2026, 9, 21 + i) for i in range(5)]  # lun 21 … vie 25, nada después
    assert last_occurrence(date(2026, 9, 21), rule, start_time=time(15, 10)) == date(2026, 9, 25)
    assert describe(rule).endswith("hasta 2026-09-25")
    assert until_local("RRULE:FREQ=DAILY;UNTIL=20260925") == "RRULE:FREQ=DAILY;UNTIL=20260925"
