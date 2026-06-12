"""Backfill de adjuntos: re-baja por IMAP (fake) los bytes de un correo ya ingerido sin media.

`ImapConfig`/`ImapClient` se stubean (sin red ni env IMAP) y el ObjectStore es fake (sin MinIO).
Cubre: crea 1 `media_asset` pending sin duplicar el inbox; idempotente (2da corrida = 0); y skip de
inbox sin adjuntos declarados.
"""

from __future__ import annotations

import email
import io
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import pytest
from sqlalchemy import text

from memex.api.object_store import set_object_store
from memex.backfill.media import backfill_inbox_media
from memex.db import connection

INTERNALDATE = datetime(2026, 5, 23, 10, 0, tzinfo=UTC)


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        # >1 KiB para superar el piso de _extract_media (DEFAULT_MIN_MEDIA_BYTES): un zip de factura
        # real nunca pesa ~139 B, y por debajo del piso el parser lo saltea (tracking pixel/logo).
        z.writestr("nota.txt", b"contenido interno del zip\n" * 64)
    return buf.getvalue()


def _email_with_zip() -> Any:
    msg = MIMEMultipart()
    msg["From"] = "Tienda <ventas@example.com>"
    msg["Subject"] = "factura"
    msg["Date"] = "Mon, 23 May 2026 10:00:00 +0000"
    msg["Message-ID"] = "<f1@example.com>"
    msg.attach(MIMEText("adjuntamos la factura", "plain"))
    z = MIMEApplication(_zip_bytes(), _subtype="zip")
    z.add_header("Content-Disposition", "attachment", filename="factura.zip")
    msg.attach(z)
    return email.message_from_bytes(msg.as_bytes())


class _FakeMsg:
    def __init__(self, obj: Any) -> None:
        self.obj = obj
        self.uid = "42"
        self.flags: tuple[str, ...] = ()
        self.size = 120
        self.date = INTERNALDATE


class _FakeClient:
    def __init__(self, cfg: Any) -> None:
        pass

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def folder_uidvalidity(self, folder: str) -> int:
        return 1

    def fetch_uids(self, folder: str, uids: list[int]) -> Iterator[_FakeMsg]:
        for _ in uids:
            yield _FakeMsg(_email_with_zip())


class _StubCfg:
    server = "imap.example.com"
    max_body_bytes = 524288
    fetch_body = True
    max_attachment_bytes = 10 * 1024 * 1024


class _StubImapConfig:
    @staticmethod
    def from_source_config(cfg: dict[str, Any]) -> _StubCfg:
        return _StubCfg()


class FakeStore:
    bucket = "memex-media"

    def __init__(self) -> None:
        self.puts: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:  # pragma: no cover
        pass

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self.puts[key] = data

    def get(self, key: str) -> bytes:  # pragma: no cover
        return self.puts[key]

    def exists(self, key: str) -> bool:  # pragma: no cover
        return key in self.puts


@pytest.fixture
def fake_store() -> Iterator[FakeStore]:
    store = FakeStore()
    set_object_store(store)
    yield store
    set_object_store(None)


@pytest.fixture
def patched_imap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("memex.backfill.media.ImapConfig", _StubImapConfig)
    monkeypatch.setattr("memex.backfill.media.ImapClient", _FakeClient)


def _new_imap_source(name: str) -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'imap') RETURNING id"),
            {"n": name},
        ).scalar_one()
    return int(sid)


def _seed_inbox_with_zip() -> int:
    payload = {
        "folder": "INBOX",
        "subject": "factura",
        "attachments": [
            {"filename": "factura.zip", "content_type": "application/zip", "size": 120}
        ],
    }
    sid = _new_imap_source("imap-bf")
    with connection() as c:
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :s, :ext, NOW(), CAST(:p AS JSONB)) RETURNING id"
            ),
            {"s": sid, "ext": "imap:imap.example.com:1:42", "p": json.dumps(payload)},
        ).scalar_one()
    return int(iid)


def test_backfill_creates_media_idempotent(patched_imap: None, fake_store: FakeStore) -> None:
    iid = _seed_inbox_with_zip()
    stats = backfill_inbox_media(1, [iid])
    assert stats.targets == 1 and stats.messages == 1 and stats.assets_created == 1

    with connection() as c:
        rows = (
            c.execute(
                text(
                    "SELECT content_type, extension, ocr_status FROM media_assets WHERE inbox_id=:i"
                ),
                {"i": iid},
            )
            .mappings()
            .all()
        )
        inbox_count = c.execute(text("SELECT count(*) FROM inbox")).scalar()
    assert len(rows) == 1
    assert rows[0]["content_type"] == "application/zip"
    assert rows[0]["extension"] == "zip"
    assert rows[0]["ocr_status"] == "pending"
    assert inbox_count == 1  # NO se duplicó el inbox

    # Idempotente: el target ya tiene media → 2da corrida no crea nada.
    stats2 = backfill_inbox_media(1, [iid])
    assert stats2.targets == 0 and stats2.assets_created == 0


def test_backfill_skips_inbox_without_attachments(
    patched_imap: None, fake_store: FakeStore
) -> None:
    sid = _new_imap_source("imap-bf2")
    with connection() as c:
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :s, 'imap:x:1:7', NOW(), CAST('{}' AS JSONB)) RETURNING id"
            ),
            {"s": sid},
        ).scalar_one()
    stats = backfill_inbox_media(1, [int(iid)])
    assert stats.targets == 0 and stats.assets_created == 0
