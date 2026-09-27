"""Descompresión y clasificación de adjuntos ZIP para el OCR (pyzipper).

Módulo PURO y SÍNCRONO (sin red ni DB): abre el ZIP, opcionalmente lo destraba con el pool de
contraseñas, y devuelve sus entradas clasificadas (imagen / PDF / texto) para que el worker las
OCR-ee. NO hace llamadas de OCR. Usa pyzipper (lee AES y ZipCrypto; superset de la stdlib zipfile).

Seguridad (zip-bomb): topes OBLIGATORIOS de nº de entradas, tamaño por entrada y tamaño total
descomprimido; se lee con un cap duro por entrada (NO se confía en el `file_size` declarado). SIN
recursión: un .zip anidado se saltea. Las contraseñas NUNCA se loguean ni viajan en errores.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pyzipper

from memex.ocr.client import OcrError

ZipEntryKind = Literal["image", "pdf", "text"]

#: extensión interna → content-type (las que el cliente de visión acepta como image_url).
_IMAGE_EXT: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "tif": "image/tiff",
}
_PDF_EXT = frozenset({"pdf"})
_TEXT_EXT = frozenset({"txt", "csv", "md", "log", "tsv"})


class ZipError(OcrError):
    """Base de los errores de descompresión de ZIP. `status_code=0` (error lógico, no HTTP).

    Subclasea `OcrError` para que el `except Exception` best-effort del worker la capture y marque
    el asset `error` (reintentable hasta `MAX_OCR_ATTEMPTS`).
    """

    def __init__(self, message: str) -> None:
        super().__init__(0, message)


class ZipEncryptedError(ZipError):
    """ZIP encriptado y ninguna contraseña del pool funcionó (o cifrado no soportado)."""


class ZipCorruptError(ZipError):
    """Los bytes no son un ZIP válido / está corrupto."""


@dataclass(frozen=True)
class ZipCaps:
    """Topes (conservadores) anti-zip-bomb. Vienen de `OcrConfig.zip_caps()`."""

    max_entries: int = 20
    max_total_bytes: int = 50 * 1024 * 1024
    max_entry_bytes: int = 15 * 1024 * 1024


@dataclass(frozen=True)
class ZipEntry:
    """Una entrada del ZIP a procesar: imagen (bytes), PDF (bytes) o texto (decodificado)."""

    name: str
    kind: ZipEntryKind
    content_type: str | None = None  # solo para `image`
    data: bytes | None = None  # para `image` / `pdf`
    text: str | None = None  # para `text`


@dataclass(frozen=True)
class ZipUnpack:
    """Resultado de la fase sync: entradas a procesar + lo que se salteó."""

    entries: tuple[ZipEntry, ...]
    skipped: tuple[str, ...]  # nombres salteados (tipo no soportado, .zip anidado, over-cap)
    truncated: bool  # se cortó por max_entries o max_total_bytes


def unpack_zip(zip_bytes: bytes, *, caps: ZipCaps, passwords: Sequence[str] = ()) -> ZipUnpack:
    """Abre el ZIP, lo destraba con el pool si hace falta, y clasifica sus entradas. Puro/sync.

    Levanta `ZipCorruptError` si no abre, y `ZipEncryptedError` si está cifrado y ninguna contraseña
    del pool funciona. El worker las trata como cualquier fallo de OCR (asset → `error`).
    """
    try:
        zf = pyzipper.AESZipFile(io.BytesIO(zip_bytes))
    except Exception as e:  # BadZipFile, etc.
        raise ZipCorruptError(f"no se pudo abrir el ZIP: {e}") from e

    try:
        infos = [info for info in zf.infolist() if not bool(info.is_dir())]
        if _is_encrypted(infos) and not _unlock(zf, infos, passwords):
            raise ZipEncryptedError("ZIP encriptado; ninguna contraseña del pool funcionó")
        return _collect(zf, infos, caps)
    finally:
        zf.close()


def _is_encrypted(infos: list[Any]) -> bool:
    return any(int(info.flag_bits) & 0x1 for info in infos)


def _unlock(zf: Any, infos: list[Any], passwords: Sequence[str]) -> bool:
    """Prueba cada contraseña contra la entrada cifrada más chica; deja seteada la que funcione."""
    encrypted = [info for info in infos if int(info.flag_bits) & 0x1]
    if not encrypted:
        return True
    probe = min(encrypted, key=lambda info: int(info.file_size))
    for pw in passwords:
        zf.setpassword(pw.encode("utf-8"))
        try:
            with zf.open(probe) as fh:  # AES/ZipCrypto validan la contraseña al abrir/primer read
                fh.read(1)
        except Exception:  # contraseña incorrecta / cifrado no soportado → probar la siguiente
            continue
        return True
    return False


def _collect(zf: Any, infos: list[Any], caps: ZipCaps) -> ZipUnpack:
    """Lee y clasifica las entradas soportadas respetando los topes anti-zip-bomb."""
    entries: list[ZipEntry] = []
    skipped: list[str] = []
    truncated = False
    total = 0
    processed = 0

    for info in infos:
        name = str(info.filename)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        kind = _classify(ext)
        if kind is None:  # tipo no soportado (incluye .zip anidado → sin recursión)
            skipped.append(name)
            continue
        if processed >= caps.max_entries:
            truncated = True
            break
        if int(info.file_size) > caps.max_entry_bytes:  # declarado demasiado grande
            skipped.append(name)
            continue
        try:
            with zf.open(info) as fh:
                data = bytes(fh.read(caps.max_entry_bytes + 1))
        except Exception:  # lectura fallida (corrupta / MAC AES) → best-effort, saltear
            skipped.append(name)
            continue
        if len(data) > caps.max_entry_bytes:  # el file_size mintió → guard anti-bomb
            skipped.append(name)
            continue
        if total + len(data) > caps.max_total_bytes:
            truncated = True
            break
        total += len(data)
        processed += 1
        entries.append(_make_entry(name, kind, ext, data))

    return ZipUnpack(tuple(entries), tuple(skipped), truncated)


def _classify(ext: str) -> ZipEntryKind | None:
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _PDF_EXT:
        return "pdf"
    if ext in _TEXT_EXT:
        return "text"
    return None


def _make_entry(name: str, kind: ZipEntryKind, ext: str, data: bytes) -> ZipEntry:
    if kind == "text":
        return ZipEntry(name=name, kind="text", text=data.decode("utf-8", errors="replace"))
    if kind == "image":
        return ZipEntry(name=name, kind="image", content_type=_IMAGE_EXT[ext], data=data)
    return ZipEntry(name=name, kind="pdf", data=data)
