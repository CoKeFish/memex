"""Subir documentos desde el dashboard: extractos, facturas, contratos.

Es la tercera vía de entrega, junto al bot de Telegram y `memex-documentos add`. Las tres terminan
en el mismo borde de ingesta, así que un archivo subido acá es indistinguible de uno mandado por
Telegram: mismo `DocumentPayload`, mismo blob, misma identidad por contenido (subir dos veces el
mismo PDF no lo duplica).

`procesar=true` corre las etapas de lectura (OCR → clasificar → extraer) antes de responder, así la
UI puede decir cuántos datos salieron del documento en vez de dejar al usuario esperando a ciegas.
Un extracto grande puede tardar decenas de segundos: es una subida a demanda, no un request de
listado.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile

from memex.api.auth import current_user_id
from memex.api.schemas import DocumentoUploadOut
from memex.db import connection
from memex.documentos.ingest import MAX_MB, DocumentoError, entregar_bytes
from memex.documentos.procesar import procesar_documentos
from memex.logging import get_logger

from fastapi import Depends, HTTPException  # isort: skip

_log = get_logger("memex.api.documentos")

router = APIRouter(prefix="/documentos", tags=["documentos"])

UserID = Annotated[int, Depends(current_user_id)]

#: Etapas que convierten el documento en datos. `media` no hace falta: los bytes entran con el
#: registro, no hay que re-bajarlos.


@router.post("", response_model=DocumentoUploadOut)
async def subir_documento(
    user_id: UserID,
    archivo: Annotated[UploadFile, File(description="PDF, ZIP o imagen (máx. 10 MB).")],
    nota: Annotated[str, Form()] = "",
    procesar: Annotated[bool, Form()] = True,
    asunto_id: Annotated[int | None, Form()] = None,
) -> dict[str, Any]:
    """Sube UN documento. 422 si el tipo no se soporta o pesa de más (con el motivo). Con
    `asunto_id`, el documento queda como evidencia MANUAL de ese asunto (enriquecerlo con las
    diapositivas, el programa, un PDF): su texto alimenta la extracción ya colgado del asunto."""
    data = await archivo.read()
    filename = archivo.filename or "documento"
    try:
        with connection() as conn:
            entrega = entregar_bytes(
                conn, user_id, data=data, filename=filename, canal="web", nota=nota
            )
            if asunto_id is not None and entrega.inbox_id is not None:
                from memex.modules.asuntos import store as asuntos_store

                asuntos_store.add_evidence(
                    conn, user_id, asunto_id, entrega.inbox_id, method="manual", confidence=1.0
                )
    except DocumentoError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    salida: dict[str, Any] = {
        "inbox_id": entrega.inbox_id,
        "filename": entrega.filename,
        "duplicado": entrega.duplicado,
        "asunto_id": asunto_id,
        "leido": False,
        "items": 0,
        "cost_usd": 0.0,
    }
    if procesar and entrega.inbox_id is not None and not entrega.duplicado:
        result = await procesar_documentos(user_id, [entrega.inbox_id])
        etapas = result.get("results", {})
        ocr = etapas.get("ocr", {})
        extract = etapas.get("extract", {})
        salida["leido"] = bool(ocr.get("ok", 0)) and not int(ocr.get("errors", 0))
        salida["items"] = int(extract.get("items", 0))
        salida["cost_usd"] = float(extract.get("cost_usd", 0) or 0)
        if int(ocr.get("errors", 0)):
            # El caso típico: viene con contraseña y no está en el pool. Que la UI lo pueda decir.
            salida["error"] = "no se pudo leer el documento (¿tiene contraseña?)"

    _log.info(
        "documentos.upload",
        user_id=user_id,
        filename=filename,
        inbox_id=entrega.inbox_id,
        duplicado=entrega.duplicado,
        items=salida["items"],
    )
    return salida


@router.get("/limites", response_model=dict[str, Any])
async def limites(user_id: UserID) -> dict[str, Any]:
    """Qué acepta la subida: lo usa el front para filtrar el selector y avisar antes de subir."""
    from memex.documentos.ingest import BY_EXTENSION

    return {"max_mb": MAX_MB, "extensiones": sorted(BY_EXTENSION), "canal": "web"}
