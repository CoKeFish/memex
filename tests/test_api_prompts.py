"""API /prompts: listar, detalle, guardar (aplica y versiona), restablecer, volver a versión."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from memex.prompts import overrides
from memex.prompts.registry import default_text, read_target, spec, write_target

KEY = "calendar.commitment"


@pytest.fixture(autouse=True)
def _limpio() -> Iterator[None]:
    yield
    for t in spec(KEY).targets:
        write_target(t, default_text(KEY))
    overrides._mark_dirty()


def test_flujo_completo(client: Any) -> None:
    r = client.get("/prompts")
    assert r.status_code == 200
    body = r.json()
    assert "Calendario" in body["groups"]
    item = next(x for x in body["items"] if x["key"] == KEY)
    assert item["has_override"] is False and item["editable"] is True

    r = client.get(f"/prompts/{KEY}")
    assert r.status_code == 200
    d = r.json()
    assert d["default"] == d["effective"] and d["override"] is None and d["versions"] == []

    nuevo = d["default"] + "\nRegla del dueño: nada de propuestas."
    r = client.put(f"/prompts/{KEY}", json={"text": nuevo, "note": "prueba API"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["override"] == nuevo and d["effective"] == nuevo and d["has_override"] is True
    assert [v["note"] for v in d["versions"]] == ["prueba API"]
    assert read_target(spec(KEY).targets[0]) == nuevo  # aplicado en el proceso de la API

    r = client.put(f"/prompts/{KEY}", json={"text": "   ", "note": ""})
    assert r.status_code == 422
    r = client.put("/prompts/no.existe", json={"text": "x"})
    assert r.status_code == 404
    r = client.put("/prompts/principios", json={"text": "x"})
    assert r.status_code == 422  # referencia, no editable

    r = client.delete(f"/prompts/{KEY}")
    assert r.status_code == 200
    d = r.json()
    assert d["override"] is None and d["effective"] == d["default"]
    vid = next(v["id"] for v in d["versions"] if v["note"] == "prueba API")
    r = client.post(f"/prompts/{KEY}/revert/{vid}")
    assert r.status_code == 200 and r.json()["override"] == nuevo
    assert client.post(f"/prompts/{KEY}/revert/999999").status_code == 404
