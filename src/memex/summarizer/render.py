"""Render de un `inbox.payload` a texto plano, agnóstico de la fuente.

Calca el helper del spike: prueba claves comunes de email / telegram / social. El
summarizer arma el prompt con esto (el contenido ORIGINAL, nunca un resumen previo).
"""

from __future__ import annotations

from typing import Any


def render_payload(payload: dict[str, Any]) -> str:
    """`{sender}: {texto}` a partir del payload, probando las claves de cada fuente."""
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

    text = "\n".join(parts).strip()
    return f"{sender}: {text}" if sender else text
