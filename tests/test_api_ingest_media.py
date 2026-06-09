"""Wiring de media en POST /ingest/batch: sube a MinIO (fake) + inserta media_assets.

Usa el TestClient + un ObjectStore FALSO inyectado vía `set_object_store`. Cubre: upload con
key content-addressed, fila media_assets `pending` (imágenes y PDFs por igual), e inbox
duplicado → media no se reprocesa.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import text

from memex.api.object_store import set_object_store
from memex.db import connection


class FakeStore:
    bucket = "memex-media"

    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes, str]] = []

    def ensure_bucket(self) -> None:  # pragma: no cover
        pass

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self.puts.append((key, data, content_type))

    def get(self, key: str) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def exists(self, key: str) -> bool:  # pragma: no cover
        return False


@pytest.fixture
def fake_store() -> Iterator[FakeStore]:
    store = FakeStore()
    set_object_store(store)
    yield store
    set_object_store(None)


def _media_item(data: bytes, *, sha256: str, content_type: str = "image/png") -> dict[str, Any]:
    return {
        "sha256": sha256,
        "content_type": content_type,
        "filename": "f",
        "size": len(data),
        "data_b64": base64.b64encode(data).decode("ascii"),
    }


def _record(sid: int, ext: str, media: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "source_id": sid,
        "external_id": ext,
        "occurred_at": "2026-05-28T12:00:00+00:00",
        "payload": {"subject": "recibo"},
        "dedupe_keys": [],
        "media": media,
    }


def _media_rows() -> list[dict[str, Any]]:
    with connection() as c:
        rows = (
            c.execute(
                text(
                    "SELECT inbox_id, sha256, object_key, bucket, ocr_status, content_type "
                    "FROM media_assets"
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def test_ingest_uploads_blob_and_records_asset(
    client: Any, seed_source: dict[str, Any], fake_store: FakeStore
) -> None:
    sid = seed_source["id"]
    real_sha = hashlib.sha256(b"hello").hexdigest()
    # El cliente manda un sha256 MENTIROSO ('abc'): el server debe IGNORARLO y recomputar.
    body = {"records": [_record(sid, "m1", [_media_item(b"hello", sha256="abc")])]}
    resp = client.post("/ingest/batch", json=body)
    assert resp.status_code == 200
    assert resp.json()["inserted"] == 1

    # Subió el blob con key content-addressed por el sha256 REAL (no el del cliente).
    assert len(fake_store.puts) == 1
    key, data, ctype = fake_store.puts[0]
    assert key == f"media/1/{real_sha}.png"
    assert data == b"hello"
    assert ctype == "image/png"

    # Registró la referencia con el sha256 recomputado, en pending.
    rows = _media_rows()
    assert len(rows) == 1
    assert rows[0]["sha256"] == real_sha
    assert rows[0]["object_key"] == f"media/1/{real_sha}.png"
    assert rows[0]["bucket"] == "memex-media"
    assert rows[0]["ocr_status"] == "pending"


def test_server_recomputes_sha256_ignoring_client(
    client: Any, seed_source: dict[str, Any], fake_store: FakeStore
) -> None:
    """Integridad content-addressed: un sha256 falsificado por el cliente no se usa."""
    sid = seed_source["id"]
    body = {"records": [_record(sid, "m1", [_media_item(b"factura", sha256="deadbeef-mentira")])]}
    assert client.post("/ingest/batch", json=body).json()["inserted"] == 1
    rows = _media_rows()
    assert rows[0]["sha256"] == hashlib.sha256(b"factura").hexdigest()
    assert "deadbeef-mentira" not in rows[0]["object_key"]


def test_invalid_base64_errors_without_orphan_inbox(
    client: Any, seed_source: dict[str, Any], fake_store: FakeStore
) -> None:
    """base64 inválido falla el record ANTES de insertar: ni inbox huérfano ni doble-cuenta."""
    sid = seed_source["id"]
    bad = {
        "sha256": "x",
        "content_type": "image/png",
        "filename": "f",
        "size": 3,
        "data_b64": "!!nope!!",
    }
    body = {"records": [_record(sid, "m1", [bad])]}
    resp = client.post("/ingest/batch", json=body)
    assert resp.status_code == 200
    counts = resp.json()
    assert counts["errors"] == 1
    assert counts["inserted"] == 0
    # Los counts suman exactamente len(records) (no doble-cuenta inserted+error).
    assert counts["inserted"] + counts["duplicates"] + counts["errors"] + counts["filtered"] == 1
    # NO quedó inbox huérfano ni media.
    with connection() as c:
        assert c.execute(text("SELECT count(*) FROM inbox")).scalar() == 0
    assert _media_rows() == []
    assert fake_store.puts == []


def test_pdf_ingests_as_pending(
    client: Any, seed_source: dict[str, Any], fake_store: FakeStore
) -> None:
    """PDF entra `pending` (como las imágenes): el worker `memex-ocr` lo procesa luego
    (capa de texto + visión de imágenes/páginas vía pymupdf)."""
    sid = seed_source["id"]
    pdf = _media_item(b"%PDF", sha256="pdf1", content_type="application/pdf")
    body = {"records": [_record(sid, "m1", [pdf])]}
    resp = client.post("/ingest/batch", json=body)
    assert resp.status_code == 200
    rows = _media_rows()
    assert len(rows) == 1
    assert rows[0]["content_type"] == "application/pdf"
    assert rows[0]["ocr_status"] == "pending"


def test_extension_stored_from_filename_or_content_type(
    client: Any, seed_source: dict[str, Any], fake_store: FakeStore
) -> None:
    sid = seed_source["id"]
    pdf = _media_item(b"%PDF-fixture", sha256="ext1", content_type="application/pdf")
    pdf["filename"] = "factura.PDF"  # del filename, normalizada a lowercase
    img = _media_item(b"\x89PNG-fixture", sha256="ext2", content_type="image/png")
    img["filename"] = "captura"  # sin extensión → derivada del content_type
    resp = client.post("/ingest/batch", json={"records": [_record(sid, "m1", [pdf, img])]})
    assert resp.status_code == 200
    with connection() as c:
        exts = (
            c.execute(text("SELECT extension FROM media_assets ORDER BY extension")).scalars().all()
        )
    assert list(exts) == ["pdf", "png"]  # del filename y del content_type, respectivamente


def test_duplicate_inbox_skips_media(
    client: Any, seed_source: dict[str, Any], fake_store: FakeStore
) -> None:
    sid = seed_source["id"]
    body = {"records": [_record(sid, "dup", [_media_item(b"hello", sha256="abc")])]}
    first = client.post("/ingest/batch", json=body)
    assert first.json()["inserted"] == 1
    second = client.post("/ingest/batch", json=body)
    assert second.json()["duplicates"] == 1

    # El 2do POST es inbox duplicado → no se sube de nuevo ni se duplica la fila.
    assert len(fake_store.puts) == 1
    assert len(_media_rows()) == 1


def test_no_media_does_not_touch_store(
    client: Any, seed_source: dict[str, Any], fake_store: FakeStore
) -> None:
    sid = seed_source["id"]
    body = {"records": [_record(sid, "m1", [])]}
    resp = client.post("/ingest/batch", json=body)
    assert resp.json()["inserted"] == 1
    assert fake_store.puts == []
    assert _media_rows() == []


def test_gateway_ingest_persists_media(client: Any, fake_store: FakeStore) -> None:
    """El gateway transporta media (antes la descartaba en silencio): el blob se sube al
    object store y se registra en media_assets, igual que /ingest e /ingest/batch."""
    state = client.post("/gateway/plugins/media-gw/state", json={"source_type": "outlook"}).json()
    real_sha = hashlib.sha256(b"adjunto-gw").hexdigest()
    record = {
        "external_id": "g1",
        "occurred_at": "2026-05-28T12:00:00+00:00",
        "payload": {"subject": "con adjunto"},
        "dedupe_keys": [],
        "media": [_media_item(b"adjunto-gw", sha256="mentira")],
    }
    resp = client.post("/gateway/plugins/media-gw/ingest", json={"records": [record]})
    assert resp.status_code == 200
    assert resp.json() == {
        "source_id": state["source_id"],
        "inserted": 1,
        "duplicates": 0,
        "errors": 0,
        "filtered": 0,
    }
    # El blob se subió (antes: 0, se perdía) y quedó la fila con el sha256 recomputado server-side.
    assert len(fake_store.puts) == 1
    rows = _media_rows()
    assert len(rows) == 1
    assert rows[0]["sha256"] == real_sha
