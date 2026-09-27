from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from memex.core.inbox_time_repair import _shift_iso, shift_source_times
from memex.db import connection


def _source(kind: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
                {"n": f"src-{kind}", "t": kind},
            ).scalar_one()
        )


def _msg(src: int, when: datetime, date_str: str | None) -> int:
    payload: dict[str, Any] = {"subject": "x"}
    if date_str is not None:
        payload["date"] = date_str
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (1, :s, :e, :o, CAST(:p AS JSONB)) RETURNING id"
                ),
                {"s": src, "e": f"e-{when.timestamp()}", "o": when, "p": json.dumps(payload)},
            ).scalar_one()
        )


def test_shift_iso_conserva_formato_z() -> None:
    assert _shift_iso("2026-05-04T17:07:11.636000Z", timedelta(hours=5)) == (
        "2026-05-04T22:07:11.636000Z"
    )
    assert _shift_iso("basura", timedelta(hours=5)) == "basura"
    assert _shift_iso(None, timedelta(hours=5)) is None


def test_shift_source_times_es_idempotente_y_respeta_el_corte(conn: Any) -> None:
    out = _source("outlook")
    t0 = datetime(2026, 5, 4, 17, 7, 11, tzinfo=UTC)
    old = _msg(out, t0, "2026-05-04T17:07:11Z")
    newer = _msg(out, t0 + timedelta(days=200), "2026-11-20T17:07:11Z")  # después del corte
    cut = datetime(2026, 9, 6, tzinfo=UTC)
    with connection() as c:
        dry = shift_source_times(c, 1, source_id=out, hours=5, before=cut, apply=False)
        assert dry.candidates == 1 and dry.shifted == 1 and dry.already_repaired == 0
        assert (
            c.execute(text("SELECT occurred_at FROM inbox WHERE id=:i"), {"i": old}).scalar() == t0
        )
        st = shift_source_times(c, 1, source_id=out, hours=5, before=cut, apply=True)
        assert st.shifted == 1
        row = c.execute(
            text("SELECT occurred_at, payload FROM inbox WHERE id=:i"), {"i": old}
        ).one()
        assert row[0] == t0 + timedelta(hours=5)
        assert row[1]["date"] == "2026-05-04T22:07:11Z"
        assert row[1]["_repairs"][0]["kind"] == "tz_shift"
        # el posterior al corte no se toca
        assert c.execute(
            text("SELECT occurred_at FROM inbox WHERE id=:i"), {"i": newer}
        ).scalar() == t0 + timedelta(days=200)
        # segunda pasada: nada que hacer
        again = shift_source_times(c, 1, source_id=out, hours=5, before=cut, apply=True)
        assert again.shifted == 0 and again.already_repaired == 1


def test_shift_rechaza_fuentes_no_outlook_sin_force(conn: Any) -> None:
    imap = _source("imap")
    with connection() as c:
        with pytest.raises(ValueError):
            shift_source_times(c, 1, source_id=imap, hours=5, before=datetime.now(UTC), apply=False)
        st = shift_source_times(
            c, 1, source_id=imap, hours=5, before=datetime.now(UTC), apply=False, force=True
        )
        assert st.candidates == 0
