"""extract_pdf / assemble_pdf_text: rutas texto/escaneado, topes, filtros y errores.

Puro (sin DB ni red): los PDFs de fixture se generan in-memory con el propio PyMuPDF, así no hay
binarios checkeados al repo y todo es reproducible en CI.
"""

from __future__ import annotations

import pytest

from memex.ocr.pdf import (
    PdfCaps,
    PdfCorruptError,
    PdfEncryptedError,
    assemble_pdf_text,
    extract_pdf,
)
from tests.ocr._pdf_fixtures import digital_pdf, encrypted_pdf, scanned_pdf


def test_digital_text_only() -> None:
    ex = extract_pdf(digital_pdf(), caps=PdfCaps())
    assert ex.mode == "text"
    assert "FACTURA" in ex.text_layer
    assert ex.images == ()
    assert ex.page_count == 1
    assert ex.skipped_reason is None


def test_digital_with_embedded_images() -> None:
    ex = extract_pdf(digital_pdf(image_px=(300, 300)), caps=PdfCaps())
    assert ex.mode == "text"
    assert "FACTURA" in ex.text_layer
    assert len(ex.images) == 2
    assert all(img.png_bytes[:4] == b"\x89PNG" for img in ex.images)
    assert all(img.content_type == "image/png" for img in ex.images)


def test_images_over_cap_skips_image_step() -> None:
    ex = extract_pdf(digital_pdf(image_px=(300, 300, 300)), caps=PdfCaps(max_images=2))
    assert ex.mode == "skipped_images"
    assert ex.images == ()  # 3 > 2 → se omiten todas, queda solo el texto
    assert ex.skipped_reason == "images_over_cap"
    assert "FACTURA" in ex.text_layer


def test_tiny_images_filtered() -> None:
    ex = extract_pdf(digital_pdf(image_px=(300, 50)), caps=PdfCaps(min_image_px=200))
    assert ex.mode == "text"
    assert len(ex.images) == 1  # la de 50px (< 200) se ignora (logo/ícono/tracking pixel)


def test_scanned_rasterizes_pages() -> None:
    ex = extract_pdf(scanned_pdf(pages=3), caps=PdfCaps())
    assert ex.mode == "scanned"
    assert ex.text_layer == ""
    assert len(ex.images) == 3
    assert all("raster" in img.origin for img in ex.images)
    assert all(img.png_bytes[:4] == b"\x89PNG" for img in ex.images)


def test_scanned_respects_page_cap() -> None:
    ex = extract_pdf(scanned_pdf(pages=7), caps=PdfCaps(max_pages=2))
    assert ex.mode == "scanned"
    assert len(ex.images) == 2


def test_short_text_routes_to_scanned() -> None:
    # "hola" (4 chars) < text_min_chars (32) → se trata como escaneado y se rasteriza la página.
    ex = extract_pdf(digital_pdf(text="hola"), caps=PdfCaps())
    assert ex.mode == "scanned"
    assert len(ex.images) == 1


def test_text_min_chars_threshold_override() -> None:
    ex = extract_pdf(digital_pdf(text="hola"), caps=PdfCaps(text_min_chars=3))
    assert ex.mode == "text"
    assert "hola" in ex.text_layer


def test_corrupt_bytes_raise() -> None:
    with pytest.raises(PdfCorruptError):
        extract_pdf(b"esto no es un PDF", caps=PdfCaps())


def test_encrypted_pdf_raises() -> None:
    with pytest.raises(PdfEncryptedError):
        extract_pdf(encrypted_pdf(), caps=PdfCaps())


def test_assemble_orders_and_drops_empty() -> None:
    assert assemble_pdf_text("  base  ", ["", "  uno ", "dos"]) == "base\n\nuno\n\ndos"
    assert assemble_pdf_text("", []) == ""
    assert assemble_pdf_text("", ["  solo  "]) == "solo"
    assert assemble_pdf_text("solo texto", []) == "solo texto"
