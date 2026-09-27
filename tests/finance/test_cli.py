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
from memex.relations.edges import Ref
from memex.relations.graph_writer import add_edge


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


def _seed_consolidated(**over: Any) -> int:
    row = {
        "direction": "egreso",
        "status": "completada",
        "kind": "pago",
        "amount": 10000,
        "currency": "COP",
        "category": "transporte",
        "counterparty": "Uber",
        "occurred_at": "2026-03-10 12:00:00+00",
        "description": "viaje",
    }
    row.update(over)
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_finance_consolidated (user_id, direction, status, kind, "
                    "amount, currency, category, counterparty, place, occurred_at, "
                    "occurred_at_precision, "
                    "description) VALUES (1, :direction, :status, :kind, :amount, :currency, "
                    ":category, :counterparty, '', CAST(:occurred_at AS TIMESTAMPTZ), 'datetime', "
                    ":description) RETURNING id"
                ),
                row,
            ).scalar_one()
        )


def test_list_and_summary_filter_and_aggregate(capsys: pytest.CaptureFixture[str]) -> None:
    _seed_consolidated(amount=10000)
    _seed_consolidated(amount=15000, occurred_at="2026-04-02 12:00:00+00")
    _seed_consolidated(amount=5000, counterparty="Tigo", category="servicios")
    _seed_consolidated(amount=2000, kind="reembolso", direction="ingreso")  # plata que volvió
    _seed_consolidated(amount=99999, status="fallida")  # no se movió: fuera por default
    _seed_consolidated(amount=7000, occurred_at="2025-12-01 12:00:00+00")  # otro año

    assert main(["list", "--q", "uber", "--since", "2026-01-01", "--json"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["count"] == 3  # 2 pagos + 1 reembolso; la fallida y la de 2025 quedan fuera
    assert out["items"][0]["amount"] == 15000.0  # más reciente primero

    assert main(["summary", "--by", "counterparty", "--since", "2026-01-01", "--json"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    by = {r["key"]: r for r in out["items"]}
    assert by["Uber"]["egresos"] == 25000.0 and by["Uber"]["reembolsos"] == 2000.0
    assert by["Uber"]["gasto_neto"] == 23000.0 and by["Tigo"]["gasto_neto"] == 5000.0

    assert main(["summary", "--by", "month", "--q", "uber", "--status", "all", "--json"]) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    months = {r["key"]: r["count"] for r in out["items"]}
    assert months["2026-03"] == 3 and months["2026-04"] == 1 and months["2025-12"] == 1


def test_remark_dry_run_reports_without_writing(capsys: pytest.CaptureFixture[str]) -> None:
    """`remark --dry-run --json` corre sobre lo cargado y no escribe; la última línea es JSON."""
    rc = main(["remark", "--dry-run", "--json"])
    assert rc == 0
    last = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(last)
    assert payload["dry_run"] is True
    assert set(payload) >= {"rows", "pairs_now", "added", "removed", "updated", "kept_decided"}


def test_dedup_pair_flag_with_unknown_id_is_noop() -> None:
    """`dedup --pair <id inexistente>` no tiene nada que juzgar: termina bien sin tocar el LLM."""
    assert main(["dedup", "--pair", "999999999"]) == 0


# ----- rm ------------------------------------------------------------------------- #


def test_rm_deletes_consolidated_and_raw_and_graph(capsys: pytest.CaptureFixture[str]) -> None:
    cons_id = _register_consolidated(capsys)
    with connection() as conn:
        raw_ids = [
            int(r[0])
            for r in conn.execute(
                text(
                    "SELECT transaction_id FROM mod_finance_transaction_links "
                    "WHERE consolidated_id = :c"
                ),
                {"c": cons_id},
            )
        ]
        # una arista cualquiera sobre el vértice del pago (por el chokepoint), para ver que se va
        add_edge(
            conn,
            1,
            Ref("finance", cons_id),
            Ref("finance", cons_id + 1_000_000),
            producer="test",
            relation_type="prueba",
        )
        edges_before = conn.execute(
            text(
                "SELECT count(*) FROM relation_edges WHERE (src_slug = 'finance' AND src_id = :c) "
                "OR (dst_slug = 'finance' AND dst_id = :c)"
            ),
            {"c": cons_id},
        ).scalar_one()
    assert raw_ids and edges_before >= 1

    rc = main(["rm", "--id", str(cons_id), "--json"])
    assert rc == 0
    out = _last_json(capsys)
    assert out["removed"] == cons_id and out["transactions"] == raw_ids
    assert out["counterparty"] == "Rappi"

    with connection() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM mod_finance_consolidated WHERE id = :c"), {"c": cons_id}
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM mod_finance_transactions WHERE id = ANY(:ids)"),
                {"ids": raw_ids},
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM relation_edges WHERE (src_slug = 'finance' AND "
                    "src_id = :c) OR (dst_slug = 'finance' AND dst_id = :c)"
                ),
                {"c": cons_id},
            ).scalar_one()
            == 0
        )
    # ya no existe: rm de nuevo falla con mensaje, no traceback
    assert main(["rm", "--id", str(cons_id)]) == 1
    assert "no existe" in capsys.readouterr().err


def test_register_rejects_category_outside_closed_list(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Antes 'ropa y accesorios' caía a 'otros' EN SILENCIO (visto con una factura por el bot).
    with pytest.raises(SystemExit) as exc:
        main(["register", "--amount", "1", "--currency", "COP", "--category", "ropa"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_register_twice_reports_already_registered(capsys: pytest.CaptureFixture[str]) -> None:
    # La misma factura mandada dos veces al bot: FASE 1 la funde con el pago que ya estaba y el
    # CLI lo DICE (antes decía «registrada» como si fuera nueva).
    args = [
        "register",
        "--amount",
        "1045970",
        "--currency",
        "COP",
        "--counterparty",
        "PASH SAS",
        "--occurred-at",
        "2026-09-10T18:30:00-05:00",
    ]
    assert main([*args, "--json"]) == 0
    first = _last_json(capsys)
    assert first["already_registered"] is False
    assert main([*args, "--description", "otra lectura de la misma foto"]) == 0
    out = capsys.readouterr().out
    assert "ya estaba registrada" in out and f"#{first['consolidated_id']}" in out
    assert main([*args, "--json"]) == 0
    again = _last_json(capsys)
    assert again["already_registered"] is True
    assert again["consolidated_id"] == first["consolidated_id"]
