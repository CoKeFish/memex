"""Endpoint GET /transport/next-arrival: veredicto on-demand con el provider mockeado (respx).

El endpoint calcula `now` con el reloj real (no se puede inyectar por HTTP), así que el evento se
siembra RELATIVO a ahora (now + 50 min) para un veredicto estable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import text

from memex.db import connection
from memex.geo.store import PingInput, insert_pings

_TZ = ZoneInfo("America/Bogota")
_GOOGLE = "https://maps.googleapis.com"
_MATRIX = "/maps/api/distancematrix/json"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_next_arrival_leave_now(client: TestClient) -> None:
    _seed_event_at(datetime.now(_TZ) + timedelta(minutes=50))  # travel 30 + buffer 10 → slack ~10
    _seed_ping()
    body = {
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
    with respx.mock(base_url=_GOOGLE) as router:
        router.get(_MATRIX).respond(json=body)
        resp = client.get("/transport/next-arrival")
    assert resp.status_code == 200
    data = resp.json()
    assert data["upcoming"] is True
    assert data["verdict"] == "leave_now"
    assert data["travel_seconds"] == 1800
    assert data["event_id"] is not None


def test_next_arrival_upcoming_false_without_event(client: TestClient) -> None:
    resp = client.get("/transport/next-arrival")
    assert resp.status_code == 200
    assert resp.json()["upcoming"] is False
