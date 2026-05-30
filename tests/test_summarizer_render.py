"""Render de payload (sin DB ni LLM)."""

from __future__ import annotations

from memex.summarizer.render import render_payload


def test_email_render_has_subject_and_sender() -> None:
    r = render_payload({"subject": "Hola", "body_text": "texto", "from": {"name": "Ana"}})
    assert "Asunto: Hola" in r
    assert "texto" in r
    assert r.startswith("Ana:")


def test_telegram_render() -> None:
    r = render_payload({"sender": {"display_name": "Beto"}, "text": "hola grupo"})
    assert "Beto" in r and "hola grupo" in r


def test_empty_payload_renders_empty() -> None:
    assert render_payload({}) == ""


def test_all_empty_fields_render_empty() -> None:
    assert render_payload({"from": {"name": "", "email": ""}, "body_text": "", "text": ""}) == ""


def test_ocr_text_appended_to_body() -> None:
    r = render_payload(
        {"subject": "Recibo", "body_text": "Adjunto recibo", "from": {"name": "Tienda"}},
        "TOTAL: $1234\nFecha: 2026-05-30",
    )
    assert r.startswith("Tienda:")
    assert "Adjunto recibo" in r
    assert "TOTAL: $1234" in r
    assert "Fecha: 2026-05-30" in r


def test_ocr_text_alone_renders() -> None:
    # Mensaje sin body (imagen pura): el texto OCR es el único contenido.
    r = render_payload({"from": {"name": "Tienda"}}, "Solo texto en la imagen")
    assert "Solo texto en la imagen" in r


def test_empty_ocr_text_is_noop() -> None:
    # Regresión: ocr_text vacío = comportamiento previo idéntico.
    base = render_payload({"subject": "Hola", "body_text": "x", "from": {"name": "Ana"}})
    with_empty = render_payload({"subject": "Hola", "body_text": "x", "from": {"name": "Ana"}}, "")
    assert base == with_empty
