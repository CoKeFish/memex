"""GET /media/{id}: sirve/descarga el blob original desde MinIO (fake) con auth por dueño.

Usa el TestClient + un ObjectStore FALSO (con `get`) inyectado vía `set_object_store`. El blob se
ingesta por POST /ingest/batch (content-addressed) y luego se sirve por /media/{id}. Cubre: serve
inline + content-type, descarga (Content-Disposition attachment), media[] en el detalle del inbox,
y owner-check (cross-tenant → 404, inexistente → 404).
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from memex.api.object_store import set_object_store
from memex.core.inbox import insert_record
from memex.core.source import SourceRecord
from memex.db import connection


class FakeStore:
    bucket = "memex-media"

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:  # pragma: no cover
        pass

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self.blobs[key] = data

    def get(self, key: str) -> bytes:
        return self.blobs[key]

    def exists(self, key: str) -> bool:  # pragma: no cover
        return key in self.blobs


@pytest.fixture
def fake_store() -> Iterator[FakeStore]:
    store = FakeStore()
    set_object_store(store)
    yield store
    set_object_store(None)


def _media_item(data: bytes, *, content_type: str, filename: str) -> dict[str, Any]:
    return {
        "sha256": "ignored",  # el server recomputa
        "content_type": content_type,
        "filename": filename,
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


def _only_media() -> dict[str, Any]:
    with connection() as c:
        row = c.execute(text("SELECT id, inbox_id FROM media_assets LIMIT 1")).mappings().first()
    assert row is not None
    return dict(row)


def test_media_serves_blob_inline_and_download(
    client: Any, seed_source: dict[str, Any], fake_store: FakeStore
) -> None:
    sid = seed_source["id"]
    item = _media_item(b"PNGDATA", content_type="image/png", filename="captura.png")
    assert (
        client.post("/ingest/batch", json={"records": [_record(sid, "m1", [item])]}).json()[
            "inserted"
        ]
        == 1
    )
    mid = _only_media()["id"]

    r = client.get(f"/media/{mid}")
    assert r.status_code == 200
    assert r.content == b"PNGDATA"
    assert r.headers["content-type"].startswith("image/png")
    cd = r.headers["content-disposition"]
    assert cd.startswith("inline") and "captura.png" in cd

    r2 = client.get(f"/media/{mid}?download=true")
    assert r2.status_code == 200
    assert r2.headers["content-disposition"].startswith("attachment")


def test_inbox_detail_includes_media(
    client: Any, seed_source: dict[str, Any], fake_store: FakeStore
) -> None:
    sid = seed_source["id"]
    item = _media_item(b"%PDF-x", content_type="application/pdf", filename="factura.PDF")
    client.post("/ingest/batch", json={"records": [_record(sid, "m1", [item])]})
    iid = _only_media()["inbox_id"]

    media = client.get(f"/inbox/{iid}").json()["media"]
    assert len(media) == 1
    assert media[0]["extension"] == "pdf"
    assert media[0]["ocr_status"] == "pending"
    assert media[0]["filename"] == "factura.PDF"
    # La referencia interna (object_key/bucket) NO se expone en el detalle.
    assert "object_key" not in media[0] and "bucket" not in media[0]


def test_media_cross_tenant_is_404(
    client: Any, seed_source: dict[str, Any], seed_user2: int, fake_store: FakeStore
) -> None:
    with connection() as c:
        src2 = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) VALUES (:u, 's2', 'imap') RETURNING id"
            ),
            {"u": seed_user2},
        ).scalar_one()
        insert_record(
            c,
            user_id=seed_user2,
            source_id=int(src2),
            record=SourceRecord(
                external_id="u2-m1",
                occurred_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
                payload={"subject": "ajeno"},
                dedupe_keys=[],
            ),
        )
        iid2 = c.execute(
            text("SELECT id FROM inbox WHERE user_id = :u LIMIT 1"), {"u": seed_user2}
        ).scalar_one()
        mid2 = c.execute(
            text(
                "INSERT INTO media_assets "
                "(user_id, inbox_id, sha256, object_key, bucket, content_type, size_bytes) "
                "VALUES (:u, :i, 'sh2', 'k2', 'memex-media', 'image/png', 3) RETURNING id"
            ),
            {"u": seed_user2, "i": int(iid2)},
        ).scalar_one()

    assert client.get(f"/media/{int(mid2)}").status_code == 404


def test_media_nonexistent_is_404(client: Any, fake_store: FakeStore) -> None:
    assert client.get("/media/999999").status_code == 404


def test_list_media_returns_assets_with_context(
    client: Any, seed_source: dict[str, Any], fake_store: FakeStore
) -> None:
    sid = seed_source["id"]
    item = _media_item(b"PNGDATA", content_type="image/png", filename="captura.png")
    client.post("/ingest/batch", json={"records": [_record(sid, "m1", [item])]})
    iid = _only_media()["inbox_id"]

    body = client.get("/media").json()
    assert body["next_cursor"] is None
    assert len(body["items"]) == 1
    it = body["items"][0]
    assert it["inbox_id"] == iid
    assert it["subject"] == "recibo"  # viene del payload del mensaje
    assert it["ocr_status"] == "pending"
    assert it["filename"] == "captura.png"
    # La referencia interna (object_key/bucket) NO se expone.
    assert "object_key" not in it and "bucket" not in it


def test_list_media_filter_by_ocr_status(
    client: Any, seed_source: dict[str, Any], fake_store: FakeStore
) -> None:
    sid = seed_source["id"]
    item = _media_item(b"PNGDATA", content_type="image/png", filename="c.png")
    client.post("/ingest/batch", json={"records": [_record(sid, "m1", [item])]})

    assert len(client.get("/media?ocr_status=pending").json()["items"]) == 1
    assert client.get("/media?ocr_status=ok").json()["items"] == []


def test_list_media_excludes_other_tenant(
    client: Any, seed_source: dict[str, Any], seed_user2: int, fake_store: FakeStore
) -> None:
    sid = seed_source["id"]
    item = _media_item(b"PNGDATA", content_type="image/png", filename="mine.png")
    client.post("/ingest/batch", json={"records": [_record(sid, "m1", [item])]})
    with connection() as c:
        src2 = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) VALUES (:u, 's2', 'imap') RETURNING id"
            ),
            {"u": seed_user2},
        ).scalar_one()
        insert_record(
            c,
            user_id=seed_user2,
            source_id=int(src2),
            record=SourceRecord(
                external_id="u2-m1",
                occurred_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
                payload={"subject": "ajeno"},
                dedupe_keys=[],
            ),
        )
        iid2 = c.execute(
            text("SELECT id FROM inbox WHERE user_id = :u LIMIT 1"), {"u": seed_user2}
        ).scalar_one()
        mid2 = c.execute(
            text(
                "INSERT INTO media_assets "
                "(user_id, inbox_id, sha256, object_key, bucket, content_type, size_bytes) "
                "VALUES (:u, :i, 'sh2', 'k2', 'memex-media', 'image/png', 3) RETURNING id"
            ),
            {"u": seed_user2, "i": int(iid2)},
        ).scalar_one()

    items = client.get("/media").json()["items"]
    ids = {it["id"] for it in items}
    assert int(mid2) not in ids  # el media del otro tenant no se filtra
    assert len(items) == 1 and items[0]["filename"] == "mine.png"
