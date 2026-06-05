"""download_message_media — whitelist por kind, topes de tamaño, best-effort.

Unit puro: fakes structural-typed para el mensaje Telethon (`.photo/.video/...`,
`.file`) y el cliente (`download_media`). No toca red ni DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from memex.ingestors.telegram._media import download_message_media

_IMG_MAX = 10 * 1024 * 1024
_VID_MAX = 100 * 1024 * 1024


@dataclass
class _FakeFile:
    mime_type: str | None = None
    name: str | None = None
    size: int | None = None


class _FakeMsg:
    """Mensaje Telethon mínimo: presencia de media + `.file`."""

    def __init__(
        self,
        *,
        photo: Any = None,
        video: Any = None,
        document: Any = None,
        sticker: Any = None,
        file: _FakeFile | None = None,
    ) -> None:
        self.photo = photo
        self.video = video
        self.document = document
        self.sticker = sticker
        self.audio = None
        self.voice = None
        self.file = file


class _FakeTC:
    """Cliente fake: `download_media` configurable + cuenta de llamadas."""

    def __init__(self, *, data: bytes | None = b"\xff\xd8\xff-bytes", raises: bool = False) -> None:
        self._data = data
        self._raises = raises
        self.call_count = 0

    @property
    def called(self) -> bool:
        return self.call_count > 0

    async def download_media(self, msg: Any) -> bytes | None:
        self.call_count += 1
        if self._raises:
            raise RuntimeError("mtproto boom")
        return self._data


class _FakeLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self.events.append((event, kw))

    def names(self) -> list[str]:
        return [e for e, _ in self.events]


async def _run(msg: _FakeMsg, tc: _FakeTC, log: _FakeLog | None = None) -> Any:
    return await download_message_media(
        msg,
        tc=tc,  # type: ignore[arg-type]  # structural fake del wrapper
        max_image_bytes=_IMG_MAX,
        max_video_bytes=_VID_MAX,
        log=log if log is not None else _FakeLog(),
    )


# ---- acepta y baja ---- #


@pytest.mark.asyncio
async def test_photo_downloads_as_jpeg() -> None:
    body = b"\xff\xd8\xff" + b"photo-bytes" * 8
    msg = _FakeMsg(
        photo=object(), file=_FakeFile(mime_type="image/jpeg", name=None, size=len(body))
    )
    tc = _FakeTC(data=body)
    blobs = await _run(msg, tc)
    assert len(blobs) == 1
    blob = blobs[0]
    assert blob.content_type == "image/jpeg"
    assert blob.size == len(body)
    assert len(blob.sha256) == 64
    assert blob.filename is None  # las fotos no traen filename


@pytest.mark.asyncio
async def test_video_mp4_downloads() -> None:
    msg = _FakeMsg(
        video=object(), file=_FakeFile(mime_type="video/mp4", name="clip.mp4", size=2048)
    )
    blobs = await _run(msg, _FakeTC(data=b"video-bytes"))
    assert len(blobs) == 1
    assert blobs[0].content_type == "video/mp4"
    assert blobs[0].filename == "clip.mp4"


@pytest.mark.asyncio
async def test_document_image_downloads() -> None:
    msg = _FakeMsg(
        document=object(), file=_FakeFile(mime_type="image/png", name="recibo.png", size=4096)
    )
    blobs = await _run(msg, _FakeTC(data=b"png-bytes"))
    assert len(blobs) == 1
    assert blobs[0].content_type == "image/png"


@pytest.mark.asyncio
async def test_document_pdf_downloads() -> None:
    msg = _FakeMsg(
        document=object(),
        file=_FakeFile(mime_type="application/pdf", name="factura.pdf", size=4096),
    )
    blobs = await _run(msg, _FakeTC(data=b"%PDF-1.4 bytes"))
    assert len(blobs) == 1
    assert blobs[0].content_type == "application/pdf"


# ---- ignora (whitelist) ---- #


@pytest.mark.asyncio
async def test_video_non_whitelisted_mime_skipped() -> None:
    msg = _FakeMsg(video=object(), file=_FakeFile(mime_type="video/x-flv", size=2048))
    tc = _FakeTC()
    log = _FakeLog()
    assert await _run(msg, tc, log) == []
    assert not tc.called
    assert "telegram.media.skipped" in log.names()


@pytest.mark.asyncio
async def test_document_zip_skipped() -> None:
    msg = _FakeMsg(document=object(), file=_FakeFile(mime_type="application/zip", size=2048))
    tc = _FakeTC()
    assert await _run(msg, tc) == []
    assert not tc.called


@pytest.mark.asyncio
async def test_document_without_mime_skipped() -> None:
    msg = _FakeMsg(document=object(), file=_FakeFile(mime_type=None, size=2048))
    tc = _FakeTC()
    assert await _run(msg, tc) == []
    assert not tc.called


@pytest.mark.asyncio
async def test_sticker_skipped_even_though_webp_is_image() -> None:
    # Un sticker es Document image/webp (∈ IMAGE_CONTENT_TYPES); debe cortarse ANTES
    # de la rama document para no bajarlo como imagen.
    msg = _FakeMsg(
        sticker=object(),
        document=object(),
        file=_FakeFile(mime_type="image/webp", size=2048),
    )
    tc = _FakeTC()
    assert await _run(msg, tc) == []
    assert not tc.called


@pytest.mark.asyncio
async def test_no_file_returns_empty() -> None:
    msg = _FakeMsg(file=None)  # texto / encuesta / geo / contacto
    tc = _FakeTC()
    assert await _run(msg, tc) == []
    assert not tc.called


# ---- topes de tamaño ---- #


@pytest.mark.asyncio
async def test_size_precheck_skips_before_download() -> None:
    msg = _FakeMsg(photo=object(), file=_FakeFile(mime_type="image/jpeg", size=_IMG_MAX + 1))
    tc = _FakeTC()
    log = _FakeLog()
    assert await _run(msg, tc, log) == []
    assert not tc.called  # no se gastó la descarga
    assert "telegram.media.too_large" in log.names()


@pytest.mark.asyncio
async def test_postcheck_skips_when_size_unknown() -> None:
    # file.size None → pre-check no aplica; el blob real supera el tope → post-check.
    msg = _FakeMsg(photo=object(), file=_FakeFile(mime_type="image/jpeg", size=None))
    tc = _FakeTC(data=b"x" * 32)
    log = _FakeLog()
    out = await download_message_media(
        msg,
        tc=tc,  # type: ignore[arg-type]
        max_image_bytes=16,
        max_video_bytes=16,
        log=log,
    )
    assert out == []
    assert tc.called  # se descargó y luego se descartó
    assert "telegram.media.too_large" in log.names()


@pytest.mark.asyncio
async def test_video_uses_video_limit_not_image_limit() -> None:
    # size entre el tope de imagen y el de video → un video pasa, una imagen no.
    big = _IMG_MAX + 1
    msg = _FakeMsg(video=object(), file=_FakeFile(mime_type="video/mp4", size=big))
    blobs = await _run(msg, _FakeTC(data=b"v"))
    assert len(blobs) == 1


# ---- best-effort ---- #


@pytest.mark.asyncio
async def test_download_raises_is_swallowed() -> None:
    msg = _FakeMsg(photo=object(), file=_FakeFile(mime_type="image/jpeg", size=1024))
    tc = _FakeTC(raises=True)
    log = _FakeLog()
    assert await _run(msg, tc, log) == []
    assert "telegram.media.fetch_error" in log.names()


@pytest.mark.asyncio
async def test_download_returns_none_or_empty() -> None:
    for data in (None, b""):
        msg = _FakeMsg(photo=object(), file=_FakeFile(mime_type="image/jpeg", size=1024))
        assert await _run(msg, _FakeTC(data=data)) == []
