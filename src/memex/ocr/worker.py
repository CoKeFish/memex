"""Worker de OCR: etapa aparte `memex-ocr`. Reclama `media_assets` pendientes y los transcribe.

Server-side + async (la capa OCR es async). Trackea progreso por `ocr_status` (espeja
`inbox.processed_at`): reclama solo `pending`; `ok` no se re-procesa (idempotente); `error` queda
reintentable. El cliente OCR y el object store son inyectables (tests con fakes, sin red).

Dedup por contenido: antes de gastar una llamada de visión, busca otra fila con el mismo
`(user_id, sha256)` ya `ok` y copia su texto. La misma imagen en dos mensajes → una sola llamada.

Manejo de fallos (cada asset es independiente y reintentable):
- Falla de OCR o de descarga → se marca `error` (+attempts) y se sigue con los demás (best-effort).
- Transcripción vacía = OCR válido (imagen sin texto legible) → se guarda `''` con estado `ok`.
- Se persiste el estado en DB ANTES de registrar el costo (`llm_calls`), igual que el summarizer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text

from memex.core.media import MAX_OCR_ATTEMPTS
from memex.core.observability import record_llm_call
from memex.db import connection
from memex.logging import get_logger
from memex.ocr.client import OCRClient, OcrQuotaError
from memex.ocr.config import OcrConfig
from memex.ocr.openai_vision import OpenAIVisionClient
from memex.ocr.pdf import PdfCaps, PdfMode, assemble_pdf_text, extract_pdf
from memex.storage import MinioObjectStore, ObjectStore, StorageConfig

_log = get_logger("memex.ocr.worker")

_DEFAULT_LIMIT = 200
#: finish_reason que cuenta como transcripción completa. Cualquier otro (length, content_filter)
#: = truncada: se guarda igual (es lo mejor que hay) pero se marca para auditoría, NUNCA queda
#: indistinguible de un OCR completo. Espeja `summarizer.worker._OK_FINISH`.
_OK_FINISH = frozenset({"stop"})


@dataclass
class OcrStats:
    """Resumen de una corrida: assets OCR-eados, resueltos por dedup, truncados, y errores."""

    ok: int = 0  # marcados ok (incluye los resueltos por dedup)
    deduped: int = 0  # subconjunto de `ok` resuelto copiando otra fila (sin llamada de visión)
    truncated: int = 0  # subconjunto de `ok` con transcripción truncada (finish_reason != stop)
    errors: int = 0


@dataclass(frozen=True)
class _Asset:
    id: int
    inbox_id: int
    sha256: str
    object_key: str
    content_type: str


def _load_pending(user_id: int, source_id: int | None, limit: int) -> list[_Asset]:
    """Assets reclamables del user: `pending`, o `error` con intentos aún disponibles.

    Reclamar errores con `ocr_attempts < MAX_OCR_ATTEMPTS` los hace reintentables (errores
    transitorios: red, 5xx, MinIO temporal) sin loop infinito. Filtrable por source vía inbox.
    """
    params: dict[str, object] = {"uid": user_id, "limit": limit, "maxatt": MAX_OCR_ATTEMPTS}
    source_filter = ""
    if source_id is not None:
        source_filter = "AND i.source_id = :sid"
        params["sid"] = source_id

    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT ma.id, ma.inbox_id, ma.sha256, ma.object_key, ma.content_type
                    FROM media_assets ma
                    JOIN inbox i ON i.id = ma.inbox_id
                    WHERE ma.user_id = :uid
                      AND (ma.ocr_status = 'pending'
                           OR (ma.ocr_status = 'error' AND ma.ocr_attempts < :maxatt))
                      {source_filter}
                    ORDER BY ma.id
                    LIMIT :limit
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )
    return [
        _Asset(
            id=int(r["id"]),
            inbox_id=int(r["inbox_id"]),
            sha256=str(r["sha256"]),
            object_key=str(r["object_key"]),
            content_type=str(r["content_type"]),
        )
        for r in rows
    ]


def _find_cached_ocr(user_id: int, sha256: str) -> tuple[str, str | None] | None:
    """Texto OCR ya hecho para el mismo contenido (otra fila `ok`), o None. Dedup de trabajo."""
    with connection() as conn:
        row = conn.execute(
            text(
                """
                SELECT ocr_text, ocr_model FROM media_assets
                WHERE user_id = :uid AND sha256 = :sha AND ocr_status = 'ok'
                  AND ocr_text IS NOT NULL
                ORDER BY id
                LIMIT 1
                """
            ),
            {"uid": user_id, "sha": sha256},
        ).first()
    if row is None:
        return None
    return str(row[0]), (str(row[1]) if row[1] is not None else None)


def _mark_ok(asset_id: int, ocr_text: str, ocr_model: str | None) -> None:
    with connection() as conn:
        conn.execute(
            text(
                """
                UPDATE media_assets
                SET ocr_status = 'ok', ocr_text = :txt, ocr_model = :model,
                    ocr_error = NULL, ocr_done_at = NOW(), ocr_attempts = ocr_attempts + 1
                WHERE id = :id
                """
            ),
            {"id": asset_id, "txt": ocr_text, "model": ocr_model},
        )


def _mark_error(asset_id: int, error: str) -> None:
    with connection() as conn:
        conn.execute(
            text(
                """
                UPDATE media_assets
                SET ocr_status = 'error', ocr_error = :err, ocr_attempts = ocr_attempts + 1
                WHERE id = :id
                """
            ),
            {"id": asset_id, "err": error[:1000]},
        )


async def _process_asset(
    user_id: int,
    client: OCRClient,
    store: ObjectStore,
    asset: _Asset,
    model: str | None,
    caps: PdfCaps,
    stats: OcrStats,
) -> None:
    """OCR-ea UN asset. Lanza si la descarga o el OCR fallan (lo maneja el caller)."""
    cached = _find_cached_ocr(user_id, asset.sha256)
    if cached is not None:
        ocr_text, ocr_model = cached
        _mark_ok(asset.id, ocr_text, ocr_model)
        stats.ok += 1
        stats.deduped += 1
        _log.info(
            "ocr.dedup_hit",
            asset_id=asset.id,
            inbox_id=asset.inbox_id,
            sha256=asset.sha256[:12],
        )
        return

    data = await asyncio.to_thread(store.get, asset.object_key)
    if asset.content_type == "application/pdf":
        await _process_pdf(user_id, client, asset, model, caps, stats, data)
        return

    result = await client.ocr_image(image_bytes=data, content_type=asset.content_type, model=model)
    ocr_text = result.text.strip()  # transcripción vacía es OCR válido → se guarda '' con ok

    # Truncación (max_tokens agotado): se guarda igual (mejor que nada) pero NUNCA indistinguible
    # de un OCR completo — se loguea y se registra en el metadata del costo (auditable).
    truncated = result.finish_reason is not None and result.finish_reason not in _OK_FINISH
    if truncated:
        stats.truncated += 1
        _log.warning(
            "ocr.asset.truncated",
            asset_id=asset.id,
            inbox_id=asset.inbox_id,
            finish_reason=result.finish_reason,
            chars=len(ocr_text),
        )

    # Persistir estado ANTES del costo: nunca un costo 'ok' sin el texto guardado.
    _mark_ok(asset.id, ocr_text, result.model)
    stats.ok += 1
    record_llm_call(
        user_id=user_id,
        purpose="ocr",
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        status="ok",
        inbox_id=asset.inbox_id,
        metadata={
            "sha256": asset.sha256,
            "chars": len(ocr_text),
            "truncated": truncated,
            "finish_reason": result.finish_reason,
        },
    )
    _log.info(
        "ocr.asset.ok",
        asset_id=asset.id,
        inbox_id=asset.inbox_id,
        model=result.model,
        chars=len(ocr_text),
        truncated=truncated,
    )


def _pdf_ocr_model(mode: PdfMode, used_vision: bool, vision_model: str | None) -> str:
    """Convención greppable para `ocr_model` de un PDF (distingue las tres rutas reales).

    Sin llamada de visión (digital sin imágenes, o imágenes sobre el tope) → `pymupdf-text`.
    Digital + imágenes → `pymupdf+<modelo>`. Escaneado (rasterizado) → `pymupdf-raster+<modelo>`.
    """
    if not used_vision or vision_model is None:
        return "pymupdf-text"
    if mode == "scanned":
        return f"pymupdf-raster+{vision_model}"
    return f"pymupdf+{vision_model}"


async def _process_pdf(
    user_id: int,
    client: OCRClient,
    asset: _Asset,
    model: str | None,
    caps: PdfCaps,
    stats: OcrStats,
    pdf_bytes: bytes,
) -> None:
    """OCR-ea un PDF: capa de texto (gratis) + visión por imagen/página (acotado por `caps`).

    Fase A (sync, en thread): `extract_pdf` decide texto-vs-escaneado y produce los PNG a OCR-ear.
    Fase B (async, acá): una llamada de visión por blob, cada una con su `record_llm_call`. El asset
    es atómico: se marca `ok` con el texto COMPLETO al final; si una imagen falla a mitad, propaga y
    el caller marca todo el PDF `error` (reintentable) — nunca un PDF medio-OCR-eado marcado `ok`.
    """
    extract = await asyncio.to_thread(extract_pdf, pdf_bytes, caps=caps)

    image_texts: list[str] = []
    used_vision = False
    any_truncated = False
    vision_model: str | None = None
    for img in extract.images:
        result = await client.ocr_image(
            image_bytes=img.png_bytes, content_type=img.content_type, model=model
        )
        used_vision = True
        vision_model = result.model
        page_text = result.text.strip()
        truncated = result.finish_reason is not None and result.finish_reason not in _OK_FINISH
        any_truncated = any_truncated or truncated
        if page_text:
            image_texts.append(page_text)
        record_llm_call(
            user_id=user_id,
            purpose="ocr",
            model=result.model,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            status="ok",
            inbox_id=asset.inbox_id,
            metadata={
                "sha256": asset.sha256,
                "kind": "pdf-image",
                "origin": img.origin,
                "chars": len(page_text),
                "truncated": truncated,
                "finish_reason": result.finish_reason,
            },
        )

    combined = assemble_pdf_text(extract.text_layer, image_texts)
    if any_truncated:
        stats.truncated += 1
    # Estado ANTES de cualquier costo ya registrado: el `ok` lleva el texto combinado completo.
    _mark_ok(asset.id, combined, _pdf_ocr_model(extract.mode, used_vision, vision_model))
    stats.ok += 1
    _log.info(
        "ocr.pdf.ok",
        asset_id=asset.id,
        inbox_id=asset.inbox_id,
        mode=extract.mode,
        pages=extract.page_count,
        vision_calls=len(extract.images),
        skipped_reason=extract.skipped_reason,
        chars=len(combined),
    )


async def run_ocr(
    user_id: int,
    *,
    source_id: int | None = None,
    limit: int = _DEFAULT_LIMIT,
    model: str | None = None,
    client: OCRClient | None = None,
    store: ObjectStore | None = None,
    caps: PdfCaps | None = None,
) -> OcrStats:
    """OCR-ea las `media_assets` pendientes del user. `client`/`store`/`caps` inyectables (tests).

    Best-effort por asset: uno que falla se loguea + marca `error` y NO frena los demás. `caps`
    (topes de PDF) sale de la config del proveedor cuando construimos el cliente; con `client`
    inyectado (tests) cae a los defaults conservadores de `PdfCaps`, salvo que se pasen explícitos.
    """
    stats = OcrStats()
    assets = _load_pending(user_id, source_id, limit)
    if not assets:
        _log.info("ocr.run.empty", user_id=user_id, source_id=source_id)
        return stats

    owns_client = client is None
    if client is not None:
        active_client: OCRClient = client
        resolved_caps = caps if caps is not None else PdfCaps()
    else:
        cfg = OcrConfig.from_env(model=model)
        active_client = OpenAIVisionClient(cfg)
        resolved_caps = caps if caps is not None else cfg.pdf_caps()
    active_store: ObjectStore = (
        store if store is not None else MinioObjectStore(StorageConfig.from_env())
    )

    _log.info("ocr.run.start", user_id=user_id, source_id=source_id, assets=len(assets))
    try:
        for asset in assets:
            try:
                await _process_asset(
                    user_id, active_client, active_store, asset, model, resolved_caps, stats
                )
            except OcrQuotaError:
                # Saldo agotado: abortar la corrida. NO se marca el asset como error (no es su
                # culpa, no debe consumir intentos); el cliente se cierra en el finally.
                _log.error("ocr.run.aborted_no_quota", asset_id=asset.id, inbox_id=asset.inbox_id)
                raise
            except Exception as e:  # best-effort: un asset fallido no frena los demás
                stats.errors += 1
                _mark_error(asset.id, str(e))
                record_llm_call(
                    user_id=user_id,
                    purpose="ocr",
                    model=model or "unknown",
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_usd=Decimal("0"),
                    latency_ms=0,
                    status="error",
                    inbox_id=asset.inbox_id,
                    error_message=str(e)[:500],
                )
                _log.error(
                    "ocr.asset.failed",
                    asset_id=asset.id,
                    inbox_id=asset.inbox_id,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
    finally:
        if owns_client and isinstance(active_client, OpenAIVisionClient):
            await active_client.aclose()

    _log.info(
        "ocr.run.end",
        user_id=user_id,
        source_id=source_id,
        ok=stats.ok,
        deduped=stats.deduped,
        truncated=stats.truncated,
        errors=stats.errors,
    )
    return stats
