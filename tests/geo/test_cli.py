"""CLI memex-geo: exit codes, wiring del provider desde env, --from-point (punto X)."""

from __future__ import annotations

import pytest
import respx

from memex.geo.cli import main

GOOGLE_BASE = "https://maps.googleapis.com"
GEOCODE = "/maps/api/geocode/json"
MATRIX = "/maps/api/distancematrix/json"


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    # Aísla del .env del repo padre (load_dotenv camina hacia arriba desde el worktree).
    monkeypatch.setattr("memex.geo.cli.load_dotenv", lambda: None)
    monkeypatch.delenv("MEMEX_GEO_PROVIDER", raising=False)
    monkeypatch.delenv("MEMEX_GEO_BASE_URL", raising=False)


def test_geocode_ok(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("GMAPS_API_KEY", "GKEY")
    body = {
        "status": "OK",
        "results": [
            {
                "formatted_address": "Av X 123",
                "geometry": {"location": {"lat": -34.6, "lng": -58.4}},
            }
        ],
    }
    with respx.mock(base_url=GOOGLE_BASE) as router:
        router.get(GEOCODE).respond(json=body)
        rc = main(["geocode", "--address", "Av X 123"])
    assert rc == 0
    assert "Av X 123" in capsys.readouterr().out


def test_geocode_missing_key_exit1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GMAPS_API_KEY", raising=False)
    rc = main(["geocode", "--address", "X"])
    assert rc == 1
    assert "GMAPS_API_KEY" in capsys.readouterr().err


def test_trip_from_point_uses_x(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GMAPS_API_KEY", "GKEY")
    body = {
        "status": "OK",
        "rows": [
            {
                "elements": [
                    {"status": "OK", "duration": {"value": 1200}, "distance": {"value": 5000}}
                ]
            }
        ],
    }
    with respx.mock(base_url=GOOGLE_BASE) as router:
        route = router.get(MATRIX).respond(json=body)
        # Coordenadas con '-' van con '=' (argparse en py3.12 las trata como opción si no).
        rc = main(["trip", "--from-point=-34.60,-58.38", "--to=-34.80,-58.50"])
    assert rc == 0
    # --from-point gana: el origen del request es X (no se geocodifica nada).
    assert route.calls[0].request.url.params["origins"] == "-34.6,-58.38"
    assert "Distancia" in capsys.readouterr().out


def test_trip_invalid_from_point_exit2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAPS_API_KEY", "GKEY")
    rc = main(["trip", "--from-point=not-a-point", "--to=-34.8,-58.5"])
    assert rc == 2


def test_trip_requires_origin_exit2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAPS_API_KEY", "GKEY")
    rc = main(["trip", "--to=-34.8,-58.5"])
    assert rc == 2


# ----- places (catálogo; solo DB, sin proveedor ni key) ---------------------------- #


def _seed_catalog() -> int:
    from sqlalchemy import text

    from memex.db import connection

    with connection() as c:
        pid = int(
            c.execute(
                text(
                    "INSERT INTO geo_places (user_id, name, formatted_address, lat, lng, "
                    "provider, provider_place_id) "
                    "VALUES (1, 'Aula 301', 'Cra 7 #40-62', 4.6286, -74.065, 'google', 'P1') "
                    "RETURNING id"
                )
            ).scalar_one()
        )
        for i in range(2):
            c.execute(
                text(
                    "INSERT INTO mod_calendar_consolidated (user_id, title, starts_on, place_id) "
                    "VALUES (1, :t, DATE '2026-07-01', :p)"
                ),
                {"t": f"Clase {i}", "p": pid},
            )
    return pid


def test_places_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    monkeypatch.delenv("GMAPS_API_KEY", raising=False)  # places NO necesita key (solo DB)
    pid = _seed_catalog()
    rc = main(["places", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["count"] == 1
    assert out["items"][0]["id"] == pid
    assert out["items"][0]["name"] == "Aula 301"
    assert out["items"][0]["event_count"] == 2


def test_places_empty_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GMAPS_API_KEY", raising=False)
    rc = main(["places"])
    assert rc == 0
    assert "Sin lugares" in capsys.readouterr().out
