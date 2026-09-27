"""Datos clave de un asunto: alta con dedup por valor, quitar, y el extractor de tareas los
cuelga del asunto."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos.datos import (
    SecretDatoError,
    add_dato,
    datos_of,
    is_secret,
    remove_dato,
)


def _asunto(name: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_asuntos (user_id, name, kind, status, aliases) "
                    "VALUES (1, :n, 'programa', 'abierto', ARRAY[]::text[]) RETURNING id"
                ),
                {"n": name},
            ).scalar_one()
        )


def test_add_dedup_and_remove() -> None:
    a = _asunto(f"Curso datos {uuid4().hex[:5]}")
    with connection() as c:
        assert (
            add_dato(c, 1, a, label="Código de clase", value="QiwJN-zjIwXZ", source_inbox_ids=[1])
            is True
        )
        assert add_dato(c, 1, a, label="", value="  qiwjn-zjiwxz ", source_inbox_ids=[2]) is False
        datos = datos_of(c, 1, a)
        assert len(datos) == 1
        assert datos[0]["label"] == "Código de clase" and datos[0]["value"] == "QiwJN-zjIwXZ"
        assert datos[0]["inbox_ids"] == [1, 2]
        assert add_dato(c, 1, a, label="Salón", value="314", origin="manual") is True
        assert [d["value"] for d in datos_of(c, 1, a)] == ["QiwJN-zjIwXZ", "314"]
        assert remove_dato(c, 1, a, "314") is True
        assert remove_dato(c, 1, a, "314") is False
        assert [d["value"] for d in datos_of(c, 1, a)] == ["QiwJN-zjIwXZ"]
        with pytest.raises(ValueError):
            add_dato(c, 1, a, label="x", value="   ")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    from fastapi.testclient import TestClient

    from memex.api.app import app
    from memex.config import settings

    monkeypatch.setattr(settings, "auth_enforced", False)
    monkeypatch.setattr(settings, "api_token", "")
    return TestClient(app)


def test_api_datos_roundtrip(client: Any) -> None:
    a = _asunto(f"Curso datos API {uuid4().hex[:5]}")
    r = client.post(f"/asuntos/{a}/datos", json={"label": "Código de clase", "value": "ABC-123"})
    assert r.status_code == 200 and r.json()["created"] is True
    story = client.get(f"/asuntos/{a}").json()
    assert [d["value"] for d in story["datos"]] == ["ABC-123"]
    r = client.post(f"/asuntos/{a}/datos/remove", json={"value": "abc-123"})
    assert r.json()["removed"] is True and r.json()["datos"] == []
    assert (
        client.post("/asuntos/999999999/datos", json={"label": "x", "value": "y"}).status_code
        == 404
    )


def test_secrets_are_never_stored() -> None:
    assert is_secret("Contraseña inicial de la cuenta institucional")
    assert is_secret("Clave de acceso", "", "")
    assert is_secret("Código", "", "your one-time password is 1234")
    assert not is_secret("Código de clase de la plataforma")
    assert not is_secret("Usuario de la cuenta institucional")
    assert not is_secret("Plataforma del curso (campus virtual)")
    a = _asunto(f"Curso secretos {uuid4().hex[:5]}")
    with connection() as c:
        with pytest.raises(SecretDatoError):
            add_dato(c, 1, a, label="Contraseña inicial", value="x")
        assert datos_of(c, 1, a) == []


def test_api_rejects_secret_dato(client: Any) -> None:
    a = _asunto(f"Curso secretos API {uuid4().hex[:5]}")
    r = client.post(f"/asuntos/{a}/datos", json={"label": "PIN de la tarjeta", "value": "1234"})
    assert r.status_code == 422
