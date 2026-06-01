"""Worker run_ocr contra la DB (sembrada), con OCRClient + ObjectStore FALSOS (sin red).

Cubre: camino feliz (ok + texto + llm_calls), dedup por sha256 (una sola llamada de visión),
idempotencia (2da corrida no-op), transcripción vacía = ok '', fallo → error/reintentable,
y que los assets `skipped` (PDF) nunca se reclaman.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.core.media import MAX_OCR_ATTEMPTS
from memex.db import connection
from memex.llm import LLMUsage
from memex.ocr.client import OcrError, OcrQuotaError, OcrResult
from memex.ocr.pdf import PdfCaps
from memex.ocr.worker import run_ocr
from tests.ocr._pdf_fixtures import digital_pdf, scanned_pdf


class FakeOCR:
    """Satisface OCRClient. Cuenta llamadas; configurable: texto, fallo, finish_reason."""

    def __init__(
        self,
        text: str = "TEXTO OCR",
        *,
        fail: bool = False,
        quota: bool = False,
        finish_reason: str = "stop",
    ) -> None:
        self.calls = 0
        self._text = text
        self._fail = fail
        self._quota = quota
        self._finish = finish_reason

    async def ocr_image(
        self, *, image_bytes: bytes, content_type: str, model: str | None = None
    ) -> OcrResult:
        self.calls += 1
        if self._fail:
            raise OcrError(500, "boom")
        if self._quota:
            raise OcrQuotaError(402, "insufficient balance")
        return OcrResult(
            text=self._text,
            model=model or "fake-vision",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason=self._finish,
        )


class FakeStore:
    """Satisface el Protocol ObjectStore. Devuelve bytes por key (o fijos); cuenta los get.

    `blobs` mapea object_key → bytes para los casos que necesitan contenido real (p. ej. un PDF
    generado); las keys no mapeadas caen a unos bytes fijos (suficiente para la ruta de imagen).
    """

    bucket = "test-bucket"

    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self.gets = 0
        self._blobs = blobs or {}

    def ensure_bucket(self) -> None:  # pragma: no cover - no usado por el worker
        pass

    def put(self, key: str, data: bytes, *, content_type: str) -> None:  # pragma: no cover
        pass

    def get(self, key: str) -> bytes:
        self.gets += 1
        return self._blobs.get(key, b"\x89PNG-fake")

    def exists(self, key: str) -> bool:  # pragma: no cover
        return True


def _new_source(name: str = "imap-test", source_type: str = "imap") -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
            {"n": name, "t": source_type},
        ).scalar()
    assert sid is not None
    return int(sid)


def _seed_inbox(source_id: int, ext: str) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, NOW(), CAST('{}' AS JSONB)) RETURNING id
                """
            ),
            {"sid": source_id, "eid": ext},
        ).scalar()
    assert iid is not None
    return int(iid)


def _seed_media(
    inbox_id: int,
    *,
    sha256: str = "sha-1",
    ocr_status: str = "pending",
    content_type: str = "image/png",
) -> int:
    with connection() as c:
        mid = c.execute(
            text(
                """
                INSERT INTO media_assets
                  (user_id, inbox_id, sha256, object_key, bucket, content_type, size_bytes,
                   filename, ocr_status)
                VALUES (1, :iid, :sha, :key, 'test-bucket', :ct, 100, 'f.png', :st)
                RETURNING id
                """
            ),
            {
                "iid": inbox_id,
                "sha": sha256,
                "key": f"media/1/{sha256}",
                "ct": content_type,
                "st": ocr_status,
            },
        ).scalar()
    assert mid is not None
    return int(mid)


def _media_row(mid: int) -> dict[str, Any]:
    with connection() as c:
        row = (
            c.execute(
                text("SELECT ocr_status, ocr_text, ocr_model FROM media_assets WHERE id = :id"),
                {"id": mid},
            )
            .mappings()
            .first()
        )
    assert row is not None
    return dict(row)


def _count_llm(purpose: str = "ocr", status: str | None = None) -> int:
    q = "SELECT count(*) FROM llm_calls WHERE purpose = :p"
    params: dict[str, Any] = {"p": purpose}
    if status is not None:
        q += " AND status = :s"
        params["s"] = status
    with connection() as c:
        return int(c.execute(text(q), params).scalar() or 0)


def test_happy_path() -> None:
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    mid = _seed_media(iid)

    fake = FakeOCR(text="  hola mundo  ")
    store = FakeStore()
    stats = asyncio.run(run_ocr(1, client=fake, store=store))

    assert fake.calls == 1
    assert store.gets == 1
    assert stats.ok == 1 and stats.errors == 0
    row = _media_row(mid)
    assert row["ocr_status"] == "ok"
    assert row["ocr_text"] == "hola mundo"  # strip aplicado
    assert row["ocr_model"] == "fake-vision"
    assert _count_llm("ocr", "ok") == 1


def test_dedup_same_sha_single_vision_call() -> None:
    sid = _new_source()
    i1 = _seed_inbox(sid, "m1")
    i2 = _seed_inbox(sid, "m2")
    m1 = _seed_media(i1, sha256="dup")
    m2 = _seed_media(i2, sha256="dup")

    fake = FakeOCR(text="texto compartido")
    stats = asyncio.run(run_ocr(1, client=fake, store=FakeStore()))

    assert fake.calls == 1  # el 2do se resuelve por dedup, sin llamada de visión
    assert stats.ok == 2 and stats.deduped == 1
    assert _media_row(m1)["ocr_text"] == "texto compartido"
    assert _media_row(m2)["ocr_text"] == "texto compartido"


def test_quota_error_aborts_run() -> None:
    """402/saldo agotado aborta la corrida (se propaga). Los assets NO se marcan error (no es su
    culpa → no consumen intentos): quedan pending para la próxima corrida con saldo."""
    sid = _new_source()
    m1 = _seed_media(_seed_inbox(sid, "m1"), sha256="q-a")
    m2 = _seed_media(_seed_inbox(sid, "m2"), sha256="q-b")

    fake = FakeOCR(quota=True)
    with pytest.raises(OcrQuotaError):
        asyncio.run(run_ocr(1, client=fake, store=FakeStore()))

    assert fake.calls == 1  # abortó en el 1ro, no siguió al 2do
    assert _media_row(m1)["ocr_status"] == "pending"
    assert _media_row(m2)["ocr_status"] == "pending"


def test_idempotent_second_run_noop() -> None:
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    _seed_media(iid)

    asyncio.run(run_ocr(1, client=FakeOCR(), store=FakeStore()))
    fake2 = FakeOCR()
    stats2 = asyncio.run(run_ocr(1, client=fake2, store=FakeStore()))
    assert fake2.calls == 0  # ya no hay pendientes
    assert stats2.ok == 0


def test_empty_transcription_is_ok() -> None:
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    mid = _seed_media(iid)

    stats = asyncio.run(run_ocr(1, client=FakeOCR(text="   "), store=FakeStore()))
    assert stats.ok == 1
    row = _media_row(mid)
    assert row["ocr_status"] == "ok"
    assert row["ocr_text"] == ""


def test_failure_marks_error_and_retryable() -> None:
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    mid = _seed_media(iid)

    stats = asyncio.run(run_ocr(1, client=FakeOCR(fail=True), store=FakeStore()))
    assert stats.errors == 1 and stats.ok == 0
    assert _media_row(mid)["ocr_status"] == "error"
    assert _count_llm("ocr", "error") == 1

    # error es reintentable solo si se resetea a pending; con ok, una corrida nueva lo procesa.
    with connection() as c:
        c.execute(text("UPDATE media_assets SET ocr_status='pending' WHERE id=:i"), {"i": mid})
    again = asyncio.run(run_ocr(1, client=FakeOCR(text="ok ahora"), store=FakeStore()))
    assert again.ok == 1
    assert _media_row(mid)["ocr_text"] == "ok ahora"


def test_skipped_pdf_never_claimed() -> None:
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    mid = _seed_media(iid, sha256="pdf", ocr_status="skipped", content_type="application/pdf")

    fake = FakeOCR()
    stats = asyncio.run(run_ocr(1, client=fake, store=FakeStore()))
    assert fake.calls == 0 and stats.ok == 0
    assert _media_row(mid)["ocr_status"] == "skipped"  # intacto


def test_source_filter() -> None:
    s1 = _new_source("a")
    s2 = _new_source("b")
    i1 = _seed_inbox(s1, "m1")
    i2 = _seed_inbox(s2, "m2")
    _seed_media(i1, sha256="s1")
    m2 = _seed_media(i2, sha256="s2")

    fake = FakeOCR()
    stats = asyncio.run(run_ocr(1, source_id=s2, client=fake, store=FakeStore()))
    assert fake.calls == 1 and stats.ok == 1
    assert _media_row(m2)["ocr_status"] == "ok"


def _ocr_call_metadata() -> dict[str, Any]:
    with connection() as c:
        row = c.execute(
            text(
                "SELECT metadata FROM llm_calls WHERE purpose='ocr' AND status='ok' "
                "ORDER BY id DESC LIMIT 1"
            )
        ).scalar()
    return row if isinstance(row, dict) else {}


def test_truncation_marked_not_silent() -> None:
    """finish_reason='length' → se guarda ok pero marcado truncado (stats + log + metadata)."""
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    mid = _seed_media(iid)

    stats = asyncio.run(
        run_ocr(1, client=FakeOCR(text="recibo parcial", finish_reason="length"), store=FakeStore())
    )
    assert stats.ok == 1 and stats.truncated == 1
    assert _media_row(mid)["ocr_status"] == "ok"  # se guarda igual (mejor que nada)
    md = _ocr_call_metadata()
    assert md.get("truncated") is True  # NO indistinguible de un OCR completo
    assert md.get("finish_reason") == "length"


def test_complete_ocr_not_marked_truncated() -> None:
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    _seed_media(iid)
    stats = asyncio.run(run_ocr(1, client=FakeOCR(finish_reason="stop"), store=FakeStore()))
    assert stats.truncated == 0
    assert _ocr_call_metadata().get("truncated") is False


def test_error_is_retryable_next_run() -> None:
    """Un asset en 'error' (intentos < MAX) se RE-RECLAMA en una corrida posterior."""
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    mid = _seed_media(iid)

    asyncio.run(run_ocr(1, client=FakeOCR(fail=True), store=FakeStore()))
    assert _media_row(mid)["ocr_status"] == "error"

    fake_ok = FakeOCR(text="ahora sí")
    stats = asyncio.run(run_ocr(1, client=fake_ok, store=FakeStore()))
    assert fake_ok.calls == 1  # se reintentó sin reset manual
    assert stats.ok == 1
    assert _media_row(mid)["ocr_text"] == "ahora sí"


def test_error_terminal_after_max_attempts() -> None:
    """Pasado MAX_OCR_ATTEMPTS, un 'error' deja de reclamarse (no loop infinito)."""
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    mid = _seed_media(iid)
    with connection() as c:
        c.execute(
            text("UPDATE media_assets SET ocr_status='error', ocr_attempts=:a WHERE id=:i"),
            {"a": MAX_OCR_ATTEMPTS, "i": mid},
        )
    fake = FakeOCR()
    stats = asyncio.run(run_ocr(1, client=fake, store=FakeStore()))
    assert fake.calls == 0 and stats.ok == 0  # agotado → no se reclama


# ----- PDF: capa de texto + visión de imágenes/páginas (extiende la ruta de imagen) -------------


def _store_for(sha: str, blob: bytes) -> FakeStore:
    """FakeStore que devuelve `blob` para la object_key del media con ese sha (ver _seed_media)."""
    return FakeStore({f"media/1/{sha}": blob})


def test_pdf_text_layer_only_no_vision() -> None:
    """PDF digital: la capa de texto se extrae GRATIS (sin visión, sin filas llm_calls)."""
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    sha = "pdf-text"
    mid = _seed_media(iid, sha256=sha, content_type="application/pdf")

    fake = FakeOCR()
    stats = asyncio.run(run_ocr(1, client=fake, store=_store_for(sha, digital_pdf())))

    assert fake.calls == 0 and stats.ok == 1
    row = _media_row(mid)
    assert row["ocr_status"] == "ok"
    assert "FACTURA" in row["ocr_text"]
    assert row["ocr_model"] == "pymupdf-text"
    assert _count_llm("ocr") == 0


def test_pdf_digital_with_images_ocrs_each() -> None:
    """PDF digital con 2 imágenes: texto + una llamada de visión por imagen (1 llm_call c/u)."""
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    sha = "pdf-img"
    mid = _seed_media(iid, sha256=sha, content_type="application/pdf")

    fake = FakeOCR(text="OCRIMG")
    pdf = digital_pdf(image_px=(300, 300))
    stats = asyncio.run(run_ocr(1, client=fake, store=_store_for(sha, pdf)))

    assert fake.calls == 2 and stats.ok == 1
    row = _media_row(mid)
    assert "FACTURA" in row["ocr_text"]
    assert row["ocr_text"].count("OCRIMG") == 2
    assert row["ocr_model"] == "pymupdf+fake-vision"
    assert _count_llm("ocr", "ok") == 2


def test_pdf_scanned_rasterizes_and_ocrs() -> None:
    """PDF escaneado (sin capa de texto): se rasteriza cada página y se OCR-ea."""
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    sha = "pdf-scan"
    mid = _seed_media(iid, sha256=sha, content_type="application/pdf")

    fake = FakeOCR(text="texto del escaneo")
    stats = asyncio.run(run_ocr(1, client=fake, store=_store_for(sha, scanned_pdf(pages=2))))

    assert fake.calls == 2 and stats.ok == 1
    row = _media_row(mid)
    assert row["ocr_text"].count("texto del escaneo") == 2
    assert row["ocr_model"] == "pymupdf-raster+fake-vision"


def test_pdf_over_image_cap_keeps_text_only() -> None:
    """Más imágenes que el tope → se OMITE el paso de imágenes (queda solo el texto, sin visión)."""
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    sha = "pdf-overcap"
    mid = _seed_media(iid, sha256=sha, content_type="application/pdf")

    fake = FakeOCR(text="no debería llamarse")
    pdf = digital_pdf(image_px=(300, 300, 300))
    stats = asyncio.run(
        run_ocr(1, client=fake, store=_store_for(sha, pdf), caps=PdfCaps(max_images=2))
    )

    assert fake.calls == 0 and stats.ok == 1
    row = _media_row(mid)
    assert "FACTURA" in row["ocr_text"]
    assert row["ocr_model"] == "pymupdf-text"


def test_pdf_dedup_same_sha_single_extraction() -> None:
    """Dos mensajes con el MISMO PDF (mismo sha) → el 2do se resuelve por dedup, sin re-OCR."""
    sid = _new_source()
    i1 = _seed_inbox(sid, "m1")
    i2 = _seed_inbox(sid, "m2")
    sha = "pdf-dup"
    m1 = _seed_media(i1, sha256=sha, content_type="application/pdf")
    m2 = _seed_media(i2, sha256=sha, content_type="application/pdf")

    fake = FakeOCR(text="OCRIMG")
    pdf = digital_pdf(image_px=(300, 300))
    stats = asyncio.run(run_ocr(1, client=fake, store=_store_for(sha, pdf)))

    assert fake.calls == 2  # solo el 1ro extrae; el 2do copia (no re-OCR-ea)
    assert stats.ok == 2 and stats.deduped == 1
    assert _media_row(m1)["ocr_text"] == _media_row(m2)["ocr_text"]
    assert _media_row(m2)["ocr_model"] == "pymupdf+fake-vision"


def test_pdf_vision_failure_marks_whole_pdf_error() -> None:
    """Si una imagen falla, TODO el PDF queda `error` (reintentable) — nunca un `ok` parcial."""
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    sha = "pdf-fail"
    mid = _seed_media(iid, sha256=sha, content_type="application/pdf")

    fake = FakeOCR(fail=True)
    stats = asyncio.run(run_ocr(1, client=fake, store=_store_for(sha, scanned_pdf(pages=2))))

    assert stats.errors == 1 and stats.ok == 0
    assert _media_row(mid)["ocr_status"] == "error"
    assert _count_llm("ocr", "error") == 1


def test_pdf_quota_mid_run_aborts_without_marking() -> None:
    """402 a mitad del PDF aborta la corrida; el asset queda `pending` (no consume intento)."""
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    sha = "pdf-quota"
    mid = _seed_media(iid, sha256=sha, content_type="application/pdf")

    fake = FakeOCR(quota=True)
    with pytest.raises(OcrQuotaError):
        asyncio.run(run_ocr(1, client=fake, store=_store_for(sha, scanned_pdf(pages=2))))

    assert _media_row(mid)["ocr_status"] == "pending"


def test_pdf_corrupt_bytes_marks_error() -> None:
    """Bytes que no son un PDF válido → el asset se marca `error` (reintentable)."""
    sid = _new_source()
    iid = _seed_inbox(sid, "m1")
    sha = "pdf-corrupt"
    mid = _seed_media(iid, sha256=sha, content_type="application/pdf")

    fake = FakeOCR()
    stats = asyncio.run(run_ocr(1, client=fake, store=_store_for(sha, b"esto no es un PDF")))

    assert fake.calls == 0 and stats.errors == 1
    assert _media_row(mid)["ocr_status"] == "error"
