"""Extracción de bytes de adjuntos imagen/PDF en el parser IMAP (`extract_media`).

Función pura: se prueba con `.eml` armados a mano. Cubre captura de imagen + PDF, dedup por
content-type (texto NO se captura), skip por tamaño, y el default off (sin media).
"""

from __future__ import annotations

import base64
import email
import hashlib
from datetime import UTC, datetime
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from memex.ingestors.imap.parser import parse_email_message

SERVER = "imap.example.com"
INTERNALDATE = datetime(2026, 5, 23, 10, 0, 0, tzinfo=UTC)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload" * 4
PDF_BYTES = b"%PDF-1.4 fake pdf payload " * 4
BIG_JPEG = b"\xff\xd8\xff" + b"x" * 5000


def _build_email() -> bytes:
    msg = MIMEMultipart()
    msg["From"] = "Tienda <ventas@example.com>"
    msg["To"] = "me@gmail.com"
    msg["Subject"] = "Tu recibo"
    msg["Date"] = "Mon, 23 May 2026 10:00:00 +0000"
    msg["Message-ID"] = "<recibo1@example.com>"
    msg.attach(MIMEText("Adjuntamos tu recibo.", "plain"))

    png = MIMEImage(PNG_BYTES, _subtype="png")
    png.add_header("Content-Disposition", "attachment", filename="recibo.png")
    msg.attach(png)

    pdf = MIMEApplication(PDF_BYTES, _subtype="pdf")
    pdf.add_header("Content-Disposition", "attachment", filename="factura.pdf")
    msg.attach(pdf)

    jpeg = MIMEImage(BIG_JPEG, _subtype="jpeg")
    jpeg.add_header("Content-Disposition", "attachment", filename="grande.jpg")
    msg.attach(jpeg)

    return msg.as_bytes()


def _parse(raw: bytes, **overrides: Any) -> Any:
    msg = email.message_from_bytes(raw)
    kwargs: dict[str, Any] = {
        "server": SERVER,
        "folder": "INBOX",
        "uidvalidity": 1,
        "uid": 1,
        "internaldate": INTERNALDATE,
        "flags": [],
        "size_bytes": len(raw),
    }
    kwargs.update(overrides)
    return parse_email_message(msg, **kwargs)


def test_default_off_extracts_no_media() -> None:
    record = _parse(_build_email())  # extract_media default False
    assert record.media == []
    # La metadata de adjuntos SÍ sigue en el payload (comportamiento previo intacto).
    assert len(record.payload["attachments"]) == 3


def test_extract_media_captures_image_and_pdf() -> None:
    record = _parse(_build_email(), extract_media=True, max_attachment_bytes=1000)

    by_type = {m.content_type: m for m in record.media}
    # PNG y PDF caben (< 1000); el JPEG (5000+) se saltea por tamaño.
    assert set(by_type) == {"image/png", "application/pdf"}

    png = by_type["image/png"]
    assert png.filename == "recibo.png"
    assert png.size == len(PNG_BYTES)
    assert png.sha256 == hashlib.sha256(PNG_BYTES).hexdigest()
    assert base64.b64decode(png.data_b64) == PNG_BYTES

    pdf = by_type["application/pdf"]
    assert pdf.filename == "factura.pdf"
    assert base64.b64decode(pdf.data_b64) == PDF_BYTES


def test_oversize_skipped_but_small_kept() -> None:
    record = _parse(_build_email(), extract_media=True, max_attachment_bytes=1000)
    assert all(m.content_type != "image/jpeg" for m in record.media)


def test_large_cap_captures_all_three() -> None:
    record = _parse(_build_email(), extract_media=True, max_attachment_bytes=10 * 1024 * 1024)
    assert len(record.media) == 3
    assert {m.content_type for m in record.media} == {
        "image/png",
        "application/pdf",
        "image/jpeg",
    }
