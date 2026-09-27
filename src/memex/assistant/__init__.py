"""Asistente conversacional de memex: un canal (Telegram) + un cerebro que razona (codex) + el CLI
`memex` como única herramienta.

Reparto (ver docs/asistente-telegram.md):

- `gateway`   — la lógica del asistente independiente del transporte: allowlist, sesión por chat,
                transcripción de audio, fotos como imágenes del prompt, turno de razonamiento,
                confirmación de escrituras, espera ante cuota de codex.
- `brain`     — un turno de `codex exec` con el prompt del agente (reglas + `memex help` +
                historial) y salida estructurada (respuesta + comandos propuestos).
- `executor`  — ejecuta SOLO comandos `memex ...` confirmados por el usuario.
- `stt`       — transcripción de notas de voz detrás de un seam: API de OpenAI (default, sirve en
                un VPS) o faster-whisper local (extra `stt-local`).
- `telegram`  — el pegamento con python-telegram-bot (polling por default; webhook opcional).
- `cli`       — `memex-assistant run|turn|doctor`.
"""
