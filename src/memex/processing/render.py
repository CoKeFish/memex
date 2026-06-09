"""Render de un `inbox.payload` a texto plano, agnóstico de la fuente.

Calca el helper del spike: prueba claves comunes de email / telegram / social. Compartido por
el summarizer y los módulos de extracción — ambos arman su prompt con el contenido ORIGINAL
(nunca un resumen previo; ADR-015 §9), así que renderizan idéntico.

`ocr_text` es el texto OCR-eado de las imágenes del mensaje (etapa `memex-ocr`). Se inyecta
junto al body para que summarizer y módulos lo vean igual que `body_text`/`subject` — así datos
que solo viven en imágenes (recibos/flyers) llegan al LLM. NO se muta `inbox.payload` (es el
original inmutable); el texto viaja por separado en `WorkRow.ocr_text` y se pasa acá.

Los adjuntos DECLARADOS (`payload.attachments`) se renderizan como un manifest de una línea
(`[Adjuntos: nombre (tamaño), …]`): aunque el adjunto no se haya almacenado/OCR-eado (tipo fuera
de la whitelist, `extract_media` apagado), el LLM al menos sabe QUÉ se adjuntó — sin esto, un
correo cuyo contenido real es el adjunto resume vacío. Este módulo tiene un port TS
(`frontend/src/lib/render-payload.ts`, vista "Input al LLM"): cualquier cambio acá debe
replicarse allá; la paridad se fija con vectores de test idénticos en ambos lados.
"""

from __future__ import annotations

from typing import Any


def _format_size(n: int) -> str:
    """Tamaño legible en base 1000 con aritmética ENTERA (sin floats ni `round()`: el banker's
    rounding de Python y el redondeo de JS divergen — la paridad con el port TS exige que ambos
    hagan exactamente `(n + mitad) // unidad`)."""
    if n >= 1_000_000:
        tenths = (n + 50_000) // 100_000
        return f"{tenths // 10}.{tenths % 10} MB"
    if n >= 1_000:
        return f"{(n + 500) // 1_000} KB"
    return f"{n} B"


def _attachments_manifest(payload: dict[str, Any]) -> str:
    """`[Adjuntos: …]` desde los declarados, o "" (sin/mal formados ⇒ render previo intacto)."""
    atts = payload.get("attachments")
    if not isinstance(atts, list):
        return ""
    items: list[str] = []
    for raw in atts:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("filename") or raw.get("content_type") or "adjunto")
        size = raw.get("size")
        # bool excluido a mano (en Python es subtipo de int; el port TS exige typeof "number").
        size_n = 0
        if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0:
            size_n = int(size)
        items.append(f"{name} ({_format_size(size_n)})" if size_n > 0 else name)
    if not items:
        return ""
    return "[Adjuntos: " + ", ".join(items) + "]"


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
    manifest = _attachments_manifest(payload)
    if manifest:
        parts.append(manifest)
    if ocr_text.strip():
        parts.append(f"[Texto en imágenes adjuntas]:\n{ocr_text.strip()}")

    text = "\n".join(parts).strip()
    return f"{sender}: {text}" if sender else text
