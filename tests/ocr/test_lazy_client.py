"""Un documento con capa de texto se lee SIN llave de visión (ni costo).

El worker construía el cliente de visión por adelantado, así que sin `OCR_API_KEY` toda la corrida
moría antes de abrir el documento — incluso un PDF digital, que se lee con pymupdf sin red. Es
justo el caso de un extracto bancario o una factura. La llave se exige recién si hay que mirar una
imagen de verdad.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from memex.llm.client import LLMUsage
from memex.ocr.client import OcrResult
from memex.ocr.config import OcrConfig, OcrConfigError
from memex.ocr.worker import _LazyVisionClient

ENV_SIN_LLAVE = {"MEMEX_OCR_MODEL": "algun-modelo"}


def test_config_sin_llave_solo_si_se_pide() -> None:
    with pytest.raises(OcrConfigError, match="OCR_API_KEY"):
        OcrConfig.from_env(ENV_SIN_LLAVE)
    cfg = OcrConfig.from_env(ENV_SIN_LLAVE, require_key=False)
    assert cfg.api_key.get_secret_value() == ""
    assert cfg.default_model == "algun-modelo"  # los topes y el resto se resuelven igual
    assert cfg.pdf_caps() is not None and cfg.password_pool() == ()


def test_el_cliente_perezoso_no_se_construye_si_nadie_mira_una_imagen() -> None:
    """Construirlo es lo que fallaba: mientras no se llame a `ocr_image`, no pasa nada."""
    cfg = OcrConfig.from_env(ENV_SIN_LLAVE, require_key=False)
    lazy = _LazyVisionClient(cfg)
    assert lazy._client is None


@pytest.mark.asyncio
async def test_aclose_sin_uso_no_rompe() -> None:
    """El `finally` del worker cierra siempre; si nunca se usó, no hay nada que cerrar."""
    lazy = _LazyVisionClient(OcrConfig.from_env(ENV_SIN_LLAVE, require_key=False))
    await lazy.aclose()


@pytest.mark.asyncio
async def test_mirar_una_imagen_sin_llave_falla_con_el_mensaje_de_siempre() -> None:
    lazy = _LazyVisionClient(OcrConfig.from_env(ENV_SIN_LLAVE, require_key=False))
    with pytest.raises(OcrConfigError, match="OCR_API_KEY"):
        await lazy.ocr_image(image_bytes=b"x", content_type="image/png")


@pytest.mark.asyncio
async def test_con_llave_construye_una_sola_vez(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = OcrConfig.from_env({**ENV_SIN_LLAVE, "OCR_API_KEY": "k"})
    lazy = _LazyVisionClient(cfg)
    built: list[int] = []

    class _Fake:
        async def ocr_image(self, **kwargs: object) -> OcrResult:
            return OcrResult(
                text="ok",
                model="m",
                usage=LLMUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                cost_usd=Decimal("0"),
                latency_ms=0,
            )

        async def aclose(self) -> None:
            pass

    def _factory(config: OcrConfig) -> _Fake:
        built.append(1)
        return _Fake()

    monkeypatch.setattr("memex.ocr.worker.OpenAIVisionClient", _factory)
    assert (await lazy.ocr_image(image_bytes=b"a", content_type="image/png")).text == "ok"
    await lazy.ocr_image(image_bytes=b"b", content_type="image/png")
    assert built == [1]  # se reusa, no se reconstruye por imagen
    await lazy.aclose()


@pytest.mark.asyncio
async def test_pdf_con_texto_conserva_el_texto_sin_proveedor_de_vision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Una factura o un extracto traen logos embebidos. Sin llave, OCR-ear el logo falla; perder
    por eso TODO el texto del documento (que se leyó gratis) es lo que hacía inservible el camino.
    Se conserva el texto y queda el motivo en la traza."""
    from memex.ocr import worker as worker_mod
    from memex.ocr.pdf import PdfCaps, PdfExtract, PdfImage
    from memex.ocr.zip import ZipCaps

    extract = PdfExtract(
        "EXTRACTO\nCompra 12.345\nCompra 6.789",
        "text",
        (PdfImage(png_bytes=b"logo", content_type="image/png", origin="p1-img1"),),
        1,
    )
    monkeypatch.setattr(worker_mod, "extract_pdf", lambda *a, **k: extract)
    marcado: dict[str, str] = {}
    monkeypatch.setattr(
        worker_mod, "_mark_ok", lambda asset_id, texto, modelo: marcado.update(texto=texto)
    )
    eventos: list[dict[str, object]] = []
    monkeypatch.setattr(worker_mod, "_record_ocr_event", lambda uid, asset, ev: eventos.append(ev))

    asset = worker_mod._Asset(
        id=1, inbox_id=2, sha256="s", object_key="k", content_type="application/pdf"
    )
    stats = worker_mod.OcrStats()
    limits = worker_mod._Limits(pdf=PdfCaps(), zip=ZipCaps(), passwords=())
    sin_llave = _LazyVisionClient(OcrConfig.from_env(ENV_SIN_LLAVE, require_key=False))

    await worker_mod._process_pdf(1, sin_llave, asset, None, limits, stats, b"%PDF-fake")

    assert "EXTRACTO" in marcado["texto"] and "12.345" in marcado["texto"]
    assert stats.ok == 1
    assert eventos and eventos[0]["skipped_reason"] == "no_vision_key"
