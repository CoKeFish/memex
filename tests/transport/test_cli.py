"""CLI memex-transport next-arrival: veredicto on-demand con el provider mockeado (respx)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import respx
from sqlalchemy import text

from memex.db import connection
from memex.geo.store import PingInput, insert_pings
from memex.transport.cli import main

_TZ = ZoneInfo("America/Bogota")
_GOOGLE = "https://maps.googleapis.com"
_MATRIX = "/maps/api/distancematrix/json"
_BODY = {
    "status": "OK",
    "rows": [
        {
            "elements": [
                {
                    "status": "OK",
                    "duration": {"value": 1800},
                    "duration_in_traffic": {"value": 1800},
                    "distance": {"value": 5000},
                }
            ]
        }
    ],
}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memex.transport.cli.load_dotenv", lambda: None)
    monkeypatch.setenv("GMAPS_API_KEY", "GKEY")
    monkeypatch.delenv("MEMEX_GEO_PROVIDER", raising=False)
    monkeypatch.delenv("MEMEX_GEO_BASE_URL", raising=False)
    for k in ("MODE", "BUFFER_MIN", "LEAD_MIN", "COMPUTE_WINDOW_MIN", "HORIZON_HOURS", "TZ"):
        monkeypatch.delenv(f"MEMEX_TRANSPORT_{k}", raising=False)


def _seed_event_at(start: datetime) -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO mod_calendar_consolidated
                    (user_id, title, starts_on, start_time, location, geo_lat, geo_lng)
                VALUES (1, 'Reunión', :d, :t, 'Aula 301', 4.65, -74.05)
                """
            ),
            {"d": start.date(), "t": start.time().replace(microsecond=0)},
        )


def _seed_ping() -> None:
    with connection() as c:
        insert_pings(
            c, user_id=1, pings=[PingInput(lat=4.60, lng=-74.08, captured_at=datetime.now(_TZ))]
        )


def test_next_arrival_json(capsys: pytest.CaptureFixture[str]) -> None:
    _seed_event_at(datetime.now(_TZ) + timedelta(minutes=50))  # slack ~10 → leave_now
    _seed_ping()
    with respx.mock(base_url=_GOOGLE) as router:
        router.get(_MATRIX).respond(json=_BODY)
        rc = main(["next-arrival", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["upcoming"] is True
    assert out["verdict"] == "leave_now"
    assert out["travel_seconds"] == 1800


def test_next_arrival_no_event(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["next-arrival", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["upcoming"] is False
