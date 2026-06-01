"""Extracción de texto e imágenes de un PDF para alimentar el OCR (PyMuPDF/fitz).

Módulo PURO y SÍNCRONO: abre el PDF, decide texto-vs-escaneado y devuelve el texto-capa + la
lista de blobs PNG que el worker debe pasar por visión. NO hace red ni DB ni llamadas de OCR
(igual que `client.py`/`pricing.py`: no importa internals de memex). El worker lo corre dentro de
`asyncio.to_thread` (PyMuPDF es CPU-bound) y después hace las llamadas async de visión, una por img.

Árbol de decisión (`extract_pdf`):
- PDF con capa de texto (>= `text_min_chars`): se usa ese texto y, además, se OCR-ean las imágenes
  embebidas grandes (>= `min_image_px`). Si hay más imágenes que `max_images`, se OMITE el paso de
  imágenes y queda solo el texto.
- PDF sin capa de texto usable (escaneo / imagen): se rasterizan las primeras `max_pages` páginas a
  PNG y se OCR-ean (el texto vive en el píxel, no en una capa de texto).

Toda imagen se normaliza a PNG RGB (los endpoints de visión aceptan image_url PNG, no CMYK ni
`application/pdf` — esa era la causa raíz por la que los PDFs quedaban `skipped`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import pymupdf

from memex.ocr.client import OcrError

#: Modo con que se resolvió un PDF — útil para logging/auditoría y para el sufijo de `ocr_model`.
PdfMode = Literal["text", "skipped_images", "scanned", "empty"]


class PdfError(OcrError):
    """Base de los errores de extracción de PDF. `status_code=0` (error lógico, no HTTP).

    Subclasea `OcrError` para que el `except Exception` best-effort del worker la capture y marque
    el asset `error` (reintentable hasta `MAX_OCR_ATTEMPTS`), igual que cualquier otro fallo de OCR.
    """

    def __init__(self, message: str) -> None:
        super().__init__(0, message)


class PdfEncryptedError(PdfError):
    """El PDF está protegido con contraseña — no se puede leer su contenido."""


class PdfCorruptError(PdfError):
    """Los bytes no son un PDF válido / está corrupto (PyMuPDF no pudo abrirlo)."""


@dataclass(frozen=True)
class PdfCaps:
    """Topes (conservadores) para acotar el fan-out de costo. Vienen de `OcrConfig.pdf_caps()`."""

    max_images: int = 5
    max_pages: int = 5
    min_image_px: int = 200
    text_min_chars: int = 32
    raster_dpi: int = 150


@dataclass(frozen=True)
class PdfImage:
    """Un blob a OCR-ear (PNG) + una etiqueta de origen para logging y orden de concatenación."""

    png_bytes: bytes
    origin: str
    content_type: str = "image/png"


@dataclass(frozen=True)
class PdfExtract:
    """Resultado de la fase sync: el texto-capa + los blobs PNG que el worker debe OCR-ear."""

    text_layer: str
    mode: PdfMode
    images: tuple[PdfImage, ...]
    page_count: int
    skipped_reason: str | None = None


def extract_pdf(pdf_bytes: bytes, *, caps: PdfCaps, passwords: Sequence[str] = ()) -> PdfExtract:
    """Abre el PDF y produce su texto-capa + las imágenes a OCR-ear. Puro, sync, sin red ni DB.

    Si el PDF pide contraseña, prueba el pool `passwords` (`doc.authenticate`). Levanta
    `PdfCorruptError` si los bytes no abren y `PdfEncryptedError` si está cifrado y ninguna
    contraseña funciona; el worker las trata como cualquier fallo de OCR (asset → `error`).
    """
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:  # FileDataError, EmptyFileError, etc. — bytes no-PDF/corruptos
        raise PdfCorruptError(f"no se pudo abrir el PDF: {e}") from e

    try:
        if doc.needs_pass and not _unlock_pdf(doc, passwords):
            raise PdfEncryptedError("PDF protegido; ninguna contraseña del pool funcionó")

        page_count = int(doc.page_count)
        if page_count == 0:  # PDF válido pero sin páginas: nada que OCR-ear → texto vacío (ok)
            return PdfExtract("", "empty", (), 0, skipped_reason="no_pages")

        text_layer = "\n".join(str(page.get_text()) for page in doc).strip()

        if len(text_layer) >= caps.text_min_chars:  # DIGITAL: texto + imágenes embebidas
            images, over_cap = _collect_embedded_images(doc, caps)
            if over_cap:
                return PdfExtract(
                    text_layer, "skipped_images", (), page_count, skipped_reason="images_over_cap"
                )
            return PdfExtract(text_layer, "text", tuple(images), page_count)

        # ESCANEADO: sin capa de texto usable → rasterizar páginas y OCR-earlas.
        images = _rasterize_pages(doc, caps)
        return PdfExtract("", "scanned", tuple(images), page_count)
    finally:
        doc.close()


def _unlock_pdf(doc: pymupdf.Document, passwords: Sequence[str]) -> bool:
    """Prueba el pool contra un PDF cifrado. `authenticate` devuelve 0 si falla, !=0 si destraba."""
    return any(int(doc.authenticate(pw)) for pw in passwords)


def _collect_embedded_images(doc: pymupdf.Document, caps: PdfCaps) -> tuple[list[PdfImage], bool]:
    """Imágenes embebidas grandes (>= min_image_px), deduplicadas por xref. (imágenes, over_cap).

    Corta-circuito: apenas las imágenes que califican superan `max_images`, devuelve `over_cap=True`
    sin decodificar ninguna (en ese caso el caller se queda solo con el texto). Filtra por el tamaño
    de la imagen FUENTE (no el rectángulo en la página: una imagen chica puede estar escalada).
    """
    seen: set[int] = set()
    qualifying: list[int] = []
    for page in doc:
        for info in page.get_images(full=True):
            xref, width, height = int(info[0]), int(info[2]), int(info[3])
            if xref in seen:
                continue
            seen.add(xref)
            if width < caps.min_image_px or height < caps.min_image_px:
                continue  # logo/ícono/tracking-pixel — no aporta texto
            qualifying.append(xref)
            if len(qualifying) > caps.max_images:
                return [], True

    images: list[PdfImage] = []
    for xref in qualifying:
        png = _png_from_xref(doc, xref)
        if png is not None:  # best-effort: una imagen que no normaliza no frena al PDF
            images.append(PdfImage(png_bytes=png, origin=f"img-xref{xref}"))
    return images, False


def _png_from_xref(doc: pymupdf.Document, xref: int) -> bytes | None:
    """Imagen embebida `xref` normalizada a PNG. CMYK → RGB (PNG no soporta CMYK). None si falla."""
    try:
        pix = pymupdf.Pixmap(doc, xref)
        if pix.n - pix.alpha >= 4:  # CMYK (4 componentes de color) → convertir a RGB
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        return bytes(pix.tobytes("png"))
    except Exception:
        return None


def _rasterize_pages(doc: pymupdf.Document, caps: PdfCaps) -> list[PdfImage]:
    """Rasteriza las primeras `max_pages` páginas a PNG (dpi=`raster_dpi`). Best-effort."""
    images: list[PdfImage] = []
    for i in range(min(caps.max_pages, int(doc.page_count))):
        try:
            png = bytes(doc[i].get_pixmap(dpi=caps.raster_dpi).tobytes("png"))
        except Exception:
            continue
        images.append(PdfImage(png_bytes=png, origin=f"page{i + 1}-raster"))
    return images


def assemble_pdf_text(text_layer: str, image_texts: Sequence[str]) -> str:
    """Junta el texto-capa y las transcripciones por imagen, en orden, descartando los vacíos."""
    parts: list[str] = []
    if text_layer.strip():
        parts.append(text_layer.strip())
    parts.extend(t.strip() for t in image_texts if t.strip())
    return "\n\n".join(parts)
