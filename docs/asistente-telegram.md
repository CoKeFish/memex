# Asistente de Telegram

Un canal (Telegram) + un cerebro que razona (codex, con cadena de pensamiento) + el CLI `memex`
como única herramienta. Habla en lenguaje natural, recibe texto, notas de voz y fotos; consulta y
responde; las escrituras las **propone** y solo las ejecuta si el usuario confirma — salvo las de
la lista `MEMEX_ASSISTANT_AUTO_EXECUTE` (default `finance register`): una factura o recibo se
registra **de una**, sin esperar al usuario (pedido del dueño, 2026-09-15); el chat muestra el
resultado (`✓ memex finance register …`) y cualquier otra escritura del mismo turno sí pide botón.
Deshacer un registro: `memex finance rm --id <consolidated_id>` (borra el pago y sus crudas).

Validado antes de construirlo con el banco `experiments/agent_cli_bench.py`: 20 pedidos en
lenguaje natural, 0 banderas inventadas, 0 escrituras sin confirmar. Por eso el CLI (y no MCP) es la
superficie de herramientas.

## Piezas (`src/memex/assistant/`)

| Módulo | Qué hace |
|---|---|
| `gateway.py` | La lógica sin transporte: allowlist, sesión por chat, transcripción, foto como imagen, turno, confirmación, espera ante cuota. |
| `brain.py` | Un turno de `codex exec --json` con el prompt del agente (`memex help` + reglas + historial) y salida con esquema: `reply` + `proposed_commands`. Fotos con `-i`. |
| `executor.py` | Ejecuta SOLO comandos `memex ...` confirmados, sin shell. |
| `stt.py` | Transcripción: `openai` (API, default) / `local` (faster-whisper) / `off`. |
| `telegram.py` | python-telegram-bot: polling o webhook, descarga de media, botones de confirmación. |
| `cli.py` | `memex-assistant run | turn | doctor`. |

## Investigar: el asistente no tiene internet, `plan` sí

El cerebro por API no navega. Cuando el dueño pide «buscá toda la información», «qué tengo que
hacer para…» o pega un enlace y dice que va a participar, lo correcto es proponer
`memex asuntos plan --id <id> --context "<enlace y lo que dijo>"`: el planificador busca en la
web con codex (solo fuentes oficiales, tarda minutos) y deja los pasos como proyecto del asunto.
Si el asunto no existe, primero `memex asuntos add`. El prompt lo dice explícito (regla 6) desde
2026-09-16, cuando el bot contestó «no tengo acceso a internet» ante el programa de embajadores.

**Escrituras en prosa.** Si el modelo deja los comandos escritos en el texto («propongo esto:
memex …») sin llamar a `proponer`, la red de seguridad (`prose_writes`) los registra igual como
propuestas, validados con la misma regla; así el botón de confirmar siempre existe.

## Configuración (`MEMEX_ASSISTANT_*`, en `.env`)

| Variable | Default | Nota |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token de @BotFather (`/newbot`). Obligatorio para `run`. |
| `ALLOWED_USER_IDS` | — | Ids de Telegram autorizados, coma-separados. Vacío = onboarding: el bot solo le responde a cada quien con su id (para copiarlo acá); no razona ni escribe. |
| `TRANSPORT` | `polling` | `polling` no necesita puerto público (funciona detrás de NAT). `webhook` exige `WEBHOOK_URL` pública y el puerto `WEBHOOK_PORT` (8788) abierto. |
| `STT` | `openai` | `openai` usa `OPENAI_API_KEY` (la misma del OCR; ~USD 0,003/min). `local` usa faster-whisper (`uv sync --extra stt-local`, modelo `turbo`, CPU). `off` rechaza audios. |
| `CODEX_WAIT_S` / `CODEX_WAIT_MAX_S` | 300 / 21600 | Con codex en cuota u ocupado avisa en el chat y reintenta (regla: se espera, no se degrada). |
| `HISTORY_TURNS` | 8 | Turnos del chat que ve el modelo. |
| `MEMEX_COMMAND` | `<python del venv> -m memex.agent_cli` | Cómo el agente y el ejecutor invocan el CLI (igual en host y contenedor). |
| `WORKDIR` | cwd | Donde vive el `.env` de memex (en el contenedor, `/app`). |

Codex: la sesión de la suscripción (`codex login` en el host; en el contenedor `CODEX_HOME=/secrets/codex`).

## Correr

```bash
uv run memex-assistant doctor                 # token, allowlist, codex, transcripción, CLI
uv run memex-assistant turn "¿qué tengo mañana?"          # un turno sin Telegram
uv run memex-assistant turn --image recibo.jpg            # foto
uv run memex-assistant turn --audio nota.ogg              # nota de voz
uv run memex-assistant turn "registrá 25.000 de uber" --confirm   # propone y ejecuta
uv run memex-assistant run                    # el bot (bloquea)
docker compose --profile assistant up -d assistant        # como servicio (restart: unless-stopped)
```

## Flujo de un mensaje

1. Allowlist. 2. Lock por chat (un turno a la vez). 3. Si hay escrituras pendientes y el mensaje
es «sí»/«no» (o el botón): ejecutar / cancelar. 4. Audio → transcripción (se muestra entre
comillas en la respuesta); foto → imagen del prompt. 5. Turno de codex: consultas las ejecuta él;
escrituras vuelven en `proposed_commands`. 6. Respuesta + comandos propuestos + botones
✅/❌. Si el modelo ejecutó una escritura sin proponerla, la respuesta lo dice (⚠️ auditoría).

## VPS

Diseñado para montarse en un VPS sin cambios de código: el servicio `assistant` del compose,
polling (sin puerto) o webhook (con URL), transcripción por API (sin GPU ni RAM extra), codex en
el contenedor. Lo que hay que llevar: el token, la allowlist, `OPENAI_API_KEY` y la sesión de codex
en `secrets/codex`. Las sesiones de chat son en memoria (un reinicio las pierde; memex es la
memoria de verdad).

## Cerebro: API directa con herramientas y streaming (2026-09-09)

Medido con codex como cerebro: p50 31 s por turno (10–63 s), porque cada mensaje era una corrida
completa de un agente de código. Ahora el cerebro por default (`MEMEX_ASSISTANT_BRAIN=api`) es una
llamada de chat con herramientas a un proveedor OpenAI-compatible, DeepSeek v4-flash por default
(`llm_consumer_settings` consumer `assistant_brain`, o `MEMEX_ASSISTANT_BRAIN_PROVIDER/MODEL`):

- `memex(command)`: ejecuta consultas del CLI (`--json`) y le devuelve la salida al modelo; las
  escrituras se RECHAZAN en código (`looks_like_write`), no en el prompt.
- `proponer(commands)`: deja escrituras exactas para que el usuario las confirme; el gateway las
  ejecuta con `CommandExecutor` tras el botón, igual que antes. Las que matchean
  `auto_execute` (`<dominio> <verbo>`, ver `executor.matches_prefix`) se corren en el acto y el
  prompt le pide al modelo redactar la respuesta como hecho («Registré…»).
- Streaming: la respuesta se pinta en Telegram mientras llega (`TelegramStreamer`: primer pedazo =
  mensaje nuevo, los siguientes lo editan cada ~1,2 s; al final queda el texto completo con los
  botones). En `memex-assistant turn` los pedazos salen por la terminal.
- Codex sigue para las fotos (DeepSeek no ve imágenes) y como fallback si la API falla; con
  `MEMEX_ASSISTANT_BRAIN=codex` vuelve a ser el cerebro completo.
- Cada llamada queda en `llm_calls` con purpose `assistant_brain` (costo visible en /metricas).
