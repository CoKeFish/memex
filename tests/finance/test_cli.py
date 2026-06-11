"""CLI `memex-finance`: `help` (descubrimiento para el agente) + `show`/`set-place` (lugar del
catálogo sobre el pago consolidado). Provider fake para `--text` (sin red)."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.geo.client import GeocodeResult, GeoNotFoundError, GeoPoint
from memex.modules.finance import cli as finance_cli
from memex.modules.finance.cli import main


def test_cli_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "memex-finance" in out
    assert "register" in out
    assert "--event" in out
    assert "set-place" in out
    assert "show" in out


# ----- show / set-place ------------------------------------------------------------ #


class _FakeGeocoder:
    """Geocoder fake para `set-place --text` (cumple lo que usa `places.resolve_place`)."""

    name: ClassVar[str] = "fake"

    def __init__(self, results: dict[str, GeocodeResult | Exception]) -> None:
        self.results = results
        self.calls = 0

    async def geocode(self, address: str) -> GeocodeResult:
        self.calls += 1
        out = self.results[address]
        if isinstance(out, Exception):
            raise out
        return out

    async def aclose(self) -> None:
        return None


def _use_fake(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    monkeypatch.setattr(finance_cli, "build_provider_from_env", lambda *a, **k: provider)


def _register_consolidated(capsys: pytest.CaptureFixture[str]) -> int:
    """Registra por CLI y devuelve el `consolidated_id` del JSON (ÚLTIMA línea de stdout)."""
    rc = main(
        ["register", "--amount", "35000", "--currency", "COP", "--counterparty", "Rappi", "--json"]
    )
    assert rc == 0
    payload: dict[str, Any] = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return int(payload["consolidated_id"])


def _seed_place(name: str = "Juan Valdez Café") -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO geo_places "
                    "(user_id, name, formatted_address, lat, lng, provider, provider_place_id, "
                    " source) "
                    "VALUES (1, :n, 'Cra 7 #1-2', 4.65, -74.05, 'fake', :n, 'gps') RETURNING id"
                ),
                {"n": name},
            ).scalar_one()
        )


def _last_json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return payload


def test_set_place_by_catalog_id_and_show(capsys: pytest.CaptureFixture[str]) -> None:
    cons_id = _register_consolidated(capsys)
    place_id = _seed_place()

    rc = main(["set-place", "--id", str(cons_id), "--place-id", str(place_id), "--json"])
    assert rc == 0
    detail = _last_json(capsys)
    assert (detail["place_id"], detail["place_name"]) == (place_id, "Juan Valdez Café")

    rc = main(["show", "--id", str(cons_id)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "lugar resuelto" in out
    assert "Juan Valdez Café" in out


def test_set_place_clear(capsys: pytest.CaptureFixture[str]) -> None:
    cons_id = _register_consolidated(capsys)
    place_id = _seed_place()
    assert main(["set-place", "--id", str(cons_id), "--place-id", str(place_id)]) == 0
    capsys.readouterr()

    rc = main(["set-place", "--id", str(cons_id), "--clear", "--json"])
    assert rc == 0
    assert _last_json(capsys)["place_id"] is None


def test_set_place_by_text_resolves_against_catalog(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cons_id = _register_consolidated(capsys)
    fake = _FakeGeocoder(
        {
            "Juan Valdez Cra 7": GeocodeResult(
                point=GeoPoint(4.65, -74.05),
                formatted_address="Cra 7 #1-2",
                provider_place_id="P1",
            )
        }
    )
    _use_fake(monkeypatch, fake)

    rc = main(["set-place", "--id", str(cons_id), "--text", "Juan Valdez Cra 7", "--json"])
    assert rc == 0
    detail = _last_json(capsys)
    assert detail["place_name"] == "Juan Valdez Cra 7"  # el texto crudo nombró el lugar nuevo
    assert fake.calls == 1

    # Mismo texto otra vez: sale del caché de resoluciones (0 llamadas extra).
    rc = main(["set-place", "--id", str(cons_id), "--text", "Juan Valdez Cra 7"])
    assert rc == 0
    assert fake.calls == 1


def test_set_place_text_zero_results(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cons_id = _register_consolidated(capsys)
    _use_fake(monkeypatch, _FakeGeocoder({"ningún lado": GeoNotFoundError("ZERO_RESULTS")}))

    rc = main(["set-place", "--id", str(cons_id), "--text", "ningún lado"])
    assert rc == 1
    assert "sin resultados" in capsys.readouterr().err


def test_set_place_validates_payment_and_place(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["set-place", "--id", "999999", "--place-id", "1"]) == 1
    assert "no existe" in capsys.readouterr().err

    cons_id = _register_consolidated(capsys)
    assert main(["set-place", "--id", str(cons_id), "--place-id", "999999"]) == 1
    assert "no está en el catálogo" in capsys.readouterr().err


def test_show_missing_payment(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show", "--id", "999999"]) == 1
    assert "no existe" in capsys.readouterr().err
