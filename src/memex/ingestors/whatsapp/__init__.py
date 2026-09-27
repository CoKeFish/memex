"""WhatsApp como DISPOSITIVO VINCULADO (whatsmeow vía neonize).

Igual que abrir WhatsApp Web: memex se vincula una vez con el código de «Vincular con número de
teléfono» y queda como un dispositivo más de la cuenta. Desde ahí recibe los mensajes en vivo y, al
vincular, un tramo de historial que decide WhatsApp.

El dueño NO quiere todos sus chats: la cuenta entera llega, pero solo los chats incluidos en
`sources.config.allowed_chats` (elegidos en /cuenta, mismo formato que Telegram: `chat_id` es el
JID) pasan al inbox. Lo demás queda en una BANDEJA cruda con retención (`whatsapp_buffer`) para
poder incluir un chat más tarde sin perder lo que ya pasó.

Piezas:
- `config.py`: ruta de la sesión (`MEMEX_WA_SESSION_PATH`) y la allowlist tipada.
- `parser.py`: de un mensaje de WhatsApp (vivo o del historial) al `WhatsAppPayload` + external_id.
- `memex/whatsapp/store.py` y `runtime.py` (lado servidor, fuera de `ingestors/` por ADR-001): el
  SQL y el cliente neonize vivo dentro del proceso del API (vincular, eventos, ingesta).

Riesgo asumido por el dueño (2026-09-12): vincular un cliente no oficial viola los términos de
WhatsApp y Meta ha avisado y baneado cuentas. Mitigación: memex NUNCA envía mensajes ni presencia;
solo escucha.
"""
