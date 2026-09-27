"""Subir un documento por el dashboard.

La tercera vía de entrega (con el bot y la consola). Lo que se comprueba acá es el contrato del
endpoint: que el archivo entre con sus bytes, que el tipo no soportado se rechace con el motivo, y
que subir dos veces el mismo archivo no lo duplique.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from memex.api.object_store import set_object_store
from memex.db import connection

PDF = b"%PDF-1.4 extracto de prueba"


class _FakeStore:
    bucket = "memex-media"

    def __init__(self) -> None:
        self.puts: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:
        pass

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self.puts[key] = data

    def get(self, key: str) -> bytes:
        return self.puts[key]

    def exists(self, key: str) -> bool:
        return key in self.puts


@pytest.fixture(autouse=True)
def _store() -> Iterator[_FakeStore]:
    store = _FakeStore()
    set_object_store(store)
    yield store
    set_object_store(None)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from memex.api.app import app
    from memex.config import settings

    monkeypatch.setattr(settings, "auth_enforced", False)
    return TestClient(app)


def _subir(client: TestClient, *, name: str = "extracto.pdf", data: bytes = PDF) -> Any:
    return client.post(
        "/documentos",
        files={"archivo": (name, data, "application/pdf")},
        data={"procesar": "false"},
    )


def test_el_documento_entra_con_sus_bytes(client: TestClient, _store: _FakeStore) -> None:
    r = _subir(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filename"] == "extracto.pdf" and body["inbox_id"] is not None
    assert body["duplicado"] is False
    # El blob quedó guardado: sin eso el documento sería solo un nombre.
    assert any(data == PDF for data in _store.puts.values())
    with connection() as conn:
        canal = conn.execute(
            text("SELECT payload->>'captured_from' FROM inbox WHERE id = :id"),
            {"id": body["inbox_id"]},
        ).scalar_one()
    assert canal == "web"


def test_subirlo_dos_veces_no_lo_duplica(client: TestClient) -> None:
    primero = _subir(client, name="agosto.pdf").json()
    repetido = _subir(client, name="otro-nombre.pdf").json()
    assert primero["duplicado"] is False and repetido["duplicado"] is True


def test_tipo_no_soportado_se_rechaza_con_el_motivo(client: TestClient) -> None:
    r = client.post(
        "/documentos",
        files={"archivo": ("hoja.xlsx", b"x", "application/vnd.ms-excel")},
        data={"procesar": "false"},
    )
    assert r.status_code == 422
    assert "no soportado" in r.json()["detail"]


def test_archivo_vacio_se_rechaza(client: TestClient) -> None:
    r = _subir(client, name="vacio.pdf", data=b"")
    assert r.status_code == 422 and "vacío" in r.json()["detail"]


def test_limites_los_publica_para_la_ui(client: TestClient) -> None:
    body = client.get("/documentos/limites").json()
    assert body["max_mb"] >= 1
    assert ".pdf" in body["extensiones"] and ".zip" in body["extensiones"]


def test_documento_con_asunto_queda_como_evidencia(client: TestClient) -> None:
    with connection() as c:
        asunto_id = int(
            c.execute(
                text(
                    "INSERT INTO mod_asuntos (user_id, name, kind, status, aliases) "
                    "VALUES (1, 'Curso con slides', 'programa', 'abierto', ARRAY[]::text[]) "
                    "RETURNING id"
                )
            ).scalar_one()
        )
    r = client.post(
        "/documentos",
        files={"archivo": ("slides.pdf", PDF + b"-slides", "application/pdf")},
        data={"procesar": "false", "asunto_id": str(asunto_id)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["asunto_id"] == asunto_id and body["inbox_id"]
    with connection() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM mod_asuntos_evidence WHERE asunto_id = :a AND inbox_id = :i "
                "AND method = 'manual' AND status = 'active'"
            ),
            {"a": asunto_id, "i": body["inbox_id"]},
        ).scalar_one()
    assert n == 1


def test_max_mb_comes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from memex.documentos import ingest

    monkeypatch.delenv("MEMEX_DOCUMENTOS_MAX_MB", raising=False)
    assert ingest._max_mb_from_env() == ingest._DEFAULT_MAX_MB
    monkeypatch.setenv("MEMEX_DOCUMENTOS_MAX_MB", "120")
    assert ingest._max_mb_from_env() == 120
    monkeypatch.setenv("MEMEX_DOCUMENTOS_MAX_MB", "abc")
    assert ingest._max_mb_from_env() == ingest._DEFAULT_MAX_MB
    monkeypatch.setenv("MEMEX_DOCUMENTOS_MAX_MB", "0")
    assert ingest._max_mb_from_env() == ingest._DEFAULT_MAX_MB
