"""Render de payload (sin DB ni LLM)."""

from __future__ import annotations

from memex.processing.render import render_payload


def test_email_render_has_subject_and_sender() -> None:
    r = render_payload({"subject": "Hola", "body_text": "texto", "from": {"name": "Ana"}})
    assert "Asunto: Hola" in r
    assert "texto" in r
    assert r.startswith("Ana:")


def test_email_render_includes_sender_email() -> None:
    # El EMAIL del remitente (su dominio) es señal — debe ir en el render, no solo el nombre.
    r = render_payload(
        {"subject": "x", "body_text": "t", "from": {"name": "Jav", "email": "r@javeriana.edu.co"}}
    )
    assert r.startswith("Jav <r@javeriana.edu.co>:")


def test_email_render_email_only_when_no_name() -> None:
    r = render_payload({"body_text": "t", "from": {"email": "noreply@acme.com"}})
    assert r.startswith("noreply@acme.com:")


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


# --- Manifest de adjuntos declarados -------------------------------------------------------
# VECTORES ESPEJO de frontend/src/lib/render-payload.test.ts (port TS): no hay runner
# cross-language, la paridad se fija duplicando estos vectores — cambiar uno = cambiar el otro.


def test_attachments_manifest_full_line_parity() -> None:
    # La línea COMPLETA, no solo un fragmento: pinea separadores, orden y formato exactos.
    r = render_payload(
        {
            "from": {"name": "Ana"},
            "body_text": "hola",
            "attachments": [{"filename": "a.pdf", "size": 2500}],
        }
    )
    assert r == "Ana: hola\n[Adjuntos: a.pdf (3 KB)]"


def test_attachments_manifest_duplicates_kept() -> None:
    # Caso real (inbox 1357): el mismo xlsx declarado 2 veces — se listan ambos, sin dedup.
    r = render_payload(
        {
            "subject": "ATUNALIPA ABRIL",
            "body_text": "ver adjunto",
            "from": {"name": "Erika"},
            "attachments": [
                {"filename": "CAPTURA.xlsx", "size": 38738, "content_type": "application/vnd.x"},
                {"filename": "CAPTURA.xlsx", "size": 38738},
            ],
        }
    )
    assert "[Adjuntos: CAPTURA.xlsx (39 KB), CAPTURA.xlsx (39 KB)]" in r


def test_attachment_size_rounding_half_up_base_1000() -> None:
    # Aritmética entera (n + mitad) // unidad: half-up SIEMPRE. `round()` (banker's) daría
    # 2 KB para 2_500 y 1.2 MB para 1_250_000 — estos vectores fijan la divergencia.
    cases = [
        (999, "999 B"),
        (1_000, "1 KB"),
        (2_500, "3 KB"),
        (38_738, "39 KB"),
        (999_499, "999 KB"),
        (999_500, "1000 KB"),  # quirk asumido: el corte a MB es por tamaño crudo, no redondeado
        (1_000_000, "1.0 MB"),
        (1_250_000, "1.3 MB"),
        (1_950_000, "2.0 MB"),
        (10_400_000, "10.4 MB"),
    ]
    for size, expected in cases:
        r = render_payload({"body_text": "x", "attachments": [{"filename": "f", "size": size}]})
        assert f"[Adjuntos: f ({expected})]" in r, (size, r)


def test_attachment_name_fallbacks_and_sizeless() -> None:
    # filename → content_type → "adjunto"; el tamaño solo aparece si es > 0.
    r = render_payload(
        {
            "body_text": "x",
            "attachments": [
                {"filename": None, "content_type": "application/pdf", "size": 0},
                {"filename": "", "content_type": "", "size": -5},
                {"size": 123},
            ],
        }
    )
    assert "[Adjuntos: application/pdf, adjunto, adjunto (123 B)]" in r


def test_attachments_malformed_or_empty_keep_render_identical() -> None:
    # Regresión cero: attachments ausente / vacío / no-lista / entradas no-dict ⇒ render previo.
    base = render_payload({"subject": "Hola", "body_text": "x", "from": {"name": "Ana"}})
    for atts in ([], "no-lista", {"filename": "a"}, [42, "x", []], None):
        r = render_payload(
            {"subject": "Hola", "body_text": "x", "from": {"name": "Ana"}, "attachments": atts}
        )
        assert r == base, atts


def test_manifest_between_body_and_ocr() -> None:
    # Posición fija: body < manifest < bloque OCR (el orden es parte del contrato de paridad).
    r = render_payload(
        {
            "subject": "Recibo",
            "body_text": "va adjunto",
            "from": {"name": "Tienda"},
            "attachments": [{"filename": "recibo.png", "size": 2048}],
        },
        "TOTAL: $99",
    )
    assert r.index("va adjunto") < r.index("[Adjuntos: recibo.png (2 KB)]")
    assert r.index("[Adjuntos: recibo.png (2 KB)]") < r.index("[Texto en imágenes adjuntas]")
