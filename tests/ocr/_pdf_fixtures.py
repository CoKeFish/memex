"""Generadores de PDFs in-memory para los tests de OCR de PDF (sin binarios checkeados al repo).

No es un módulo de tests (prefijo `_`): solo helpers compartidos por `test_pdf.py` (extracción pura)
y `test_worker.py` (pipeline contra DB). Los PDFs se arman con el propio PyMuPDF → reproducibles.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence

import pymupdf
import pyzipper

#: Texto largo (> text_min_chars por defecto) para que un PDF cuente como "digital".
ENOUGH_TEXT = "FACTURA NRO 123 total 45 USD vencimiento 2026-06-01 ref ABC"


def make_png(px: int, rgb: tuple[float, float, float]) -> bytes:
    """Un PNG cuadrado de `px` px y color `rgb` (color distinto → xref distinto al embeberlo)."""
    doc = pymupdf.open()
    page = doc.new_page(width=px, height=px)
    shape = page.new_shape()
    shape.draw_rect(pymupdf.Rect(0, 0, px, px))
    shape.finish(fill=rgb, color=rgb)
    shape.commit()
    out = bytes(page.get_pixmap().tobytes("png"))
    doc.close()
    return out


def digital_pdf(text: str = ENOUGH_TEXT, image_px: Sequence[int] = ()) -> bytes:
    """PDF con capa de texto y, opcional, imágenes embebidas de los tamaños `image_px` dados."""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    if text:
        page.insert_text((50, 60), text)
    for i, px in enumerate(image_px):
        rgb = (0.1 * (i + 1), 0.2, 0.6)  # distinto por índice → imágenes distintas
        rect = pymupdf.Rect(50, 100 + i * 60, 130, 180 + i * 60)
        page.insert_image(rect, stream=make_png(px, rgb))
    out = bytes(doc.tobytes())
    doc.close()
    return out


def scanned_pdf(pages: int = 1) -> bytes:
    """PDF SIN capa de texto: cada página es una imagen a página completa (un escaneo)."""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=600, height=800)
        page.insert_image(
            pymupdf.Rect(0, 0, 600, 800), stream=make_png(400, (0.3, 0.3 + 0.1 * i, 0.4))
        )
    out = bytes(doc.tobytes())
    doc.close()
    return out


def encrypted_pdf(
    *, password: str = "secreto", full_perms: bool = False, text: str = "documento cifrado ref XYZ"
) -> bytes:
    """PDF protegido con contraseña (`needs_pass` True al reabrir).

    `full_perms` concede permisos de extracción: sin él, autenticar con la contraseña de usuario
    igual restringe `get_text` (el contenido se recupera por rasterizado/visión, no por texto).
    """
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    kwargs: dict[str, object] = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "user_pw": password}
    if full_perms:
        kwargs["permissions"] = int(
            pymupdf.PDF_PERM_ACCESSIBILITY | pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_COPY
        )
    out = bytes(doc.tobytes(**kwargs))
    doc.close()
    return out


def make_zip(files: Mapping[str, bytes], *, password: str | None = None) -> bytes:
    """Arma un ZIP in-memory con `files` (nombre→bytes). Si `password`, cifra con AES (WZ_AES)."""
    buf = io.BytesIO()
    kwargs: dict[str, object] = {"compression": pyzipper.ZIP_DEFLATED}
    if password:
        kwargs["encryption"] = pyzipper.WZ_AES
    with pyzipper.AESZipFile(buf, "w", **kwargs) as zf:
        if password:
            zf.setpassword(password.encode("utf-8"))
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()
