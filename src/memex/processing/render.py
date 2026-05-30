"""Render de un `inbox.payload` a texto plano, agnóstico de la fuente.

Calca el helper del spike: prueba claves comunes de email / telegram / social. Compartido por
el summarizer y los módulos de extracción — ambos arman su prompt con el contenido ORIGINAL
(nunca un resumen previo; ADR-015 §9), así que renderizan idéntico.

`ocr_text` es el texto OCR-eado de las imágenes del mensaje (etapa `memex-ocr`). Se inyecta
junto al body para que summarizer y módulos lo vean igual que `body_text`/`subject` — así datos
que solo viven en imágenes (recibos/flyers) llegan al LLM. NO se muta `inbox.payload` (es el
original inmutable); el texto viaja por separado en `WorkRow.ocr_text` y se pasa acá.
"""

from __future__ import annotations

from typing import Any


def render_payload(payload: dict[str, Any], ocr_text: str = "") -> str:
    """`{sender}: {texto}` a partir del payload, probando las claves de cada fuente.

    Si `ocr_text` no está vacío, se agrega como una sección al final del cuerpo.
    """
    sender = ""
    frm = payload.get("from")
    if isinstance(frm, dict):
        sender = str(frm.get("name") or frm.get("email") or "")
    snd = payload.get("sender")
    if not sender and isinstance(snd, dict):
        sender = str(snd.get("display_name") or snd.get("username") or "")
    if not sender:
        sender = str(payload.get("account") or payload.get("chat_title") or "")

    parts: list[str] = []
    subject = payload.get("subject")
    if subject:
        parts.append(f"Asunto: {subject}")
    body = payload.get("body_text") or payload.get("text") or payload.get("media_caption") or ""
    if body:
        parts.append(str(body))
    if ocr_text.strip():
        parts.append(f"[Texto en imágenes adjuntas]:\n{ocr_text.strip()}")

    text = "\n".join(parts).strip()
    return f"{sender}: {text}" if sender else text
