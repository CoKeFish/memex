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
