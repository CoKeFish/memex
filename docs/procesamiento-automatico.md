# Procesamiento automático de lo nuevo (`procesar_nuevos`)

Hasta ahora el procesamiento en dev era manual: un mensaje nuevo del grupo del curso entraba al
inbox (WhatsApp en vivo; Telegram y Notion por traída) y no generaba tareas, datos ni hitos hasta
que alguien corría el lote desde /procesamiento o la consola. El job `procesar_nuevos` del
scheduler cierra ese hueco de forma **acotada** y con **costo visible**.

## Qué hace

Cada 10 minutos (`interval_procesar_nuevos`):

1. Lee `auto_processing` (una fila por user, `GET/PATCH /processing/auto`): `enabled`, `kinds`,
   `watermark`, `max_per_run`, `daily_cost_cap_usd`.
2. Si el gasto en LLM del día (todas las corridas, `llm_calls` desde la medianoche de Bogotá) ya
   llegó al tope, **se salta** y anota `last_skip_reason = tope_diario`.
3. Toma lo recibido (`inbox.received_at`) después de la marca de agua **de cada medio**, en orden de
   llegada y con su cuota de la corrida (`max_per_run` repartido entre los medios activos). Un medio
   recién sumado arranca desde hace un día, sin que lo arrastre el tráfico de los demás: con una sola
   marca compartida, sumar el correo cuando la marca ya había avanzado con los chats dejó 158 correos
   sin mirar.
4. Corre `reprocess` con **todas** las etapas (media, ocr, classify, relevance, extract, asuntos,
   graph). Son idempotentes por cursor: repetir no re-gasta.
5. Avanza la marca de agua hasta el mensaje más nuevo procesado y deja el rastro
   (`last_run_at`, `last_targets`, `last_cost_usd`). Cada corrida es además una fila `worker_runs`
   (job `procesar_nuevos`) con stats y costo por etapa, visible en /procesamiento.

Defaults: `kinds = chat, notes` (los chats ya son solo los elegidos en /cuenta; los documentos se
procesan al subirlos; el correo sigue por lotes a mano), 40 por corrida, tope USD 1,00 por día.

## Encender y ajustar

```
PATCH /processing/scheduler   {"enabled_jobs": "finance,tareas,procesar_nuevos"}
PATCH /processing/auto        {"kinds": ["chat","notes"], "max_per_run": 40, "daily_cost_cap_usd": 1.0}
PATCH /processing/auto        {"watermarks": {"email": "2026-09-11T00:00:00Z"}}  # ese medio, desde ahí
PATCH /processing/auto        {"watermarks": {"email": null}}      # ese medio arranca de su ventana
PATCH /processing/auto        {"enabled": false}
```

Una etapa con errores no frena la marca: los errores quedan en la corrida y se reintentan a mano
(`memex-reprocess --only errored`). Una cuota agotada del proveedor sí la frena (la corrida queda
fallida y se reintenta al tick siguiente).

Cada corrida anota cuántos mensajes entraron por medio (`by_kind`) y dónde quedó cada marca.

## Si el proveedor se cuelga

Un proveedor puede aceptar la llamada, contestar `200` en las cabeceras y no mandar nunca el
cuerpo. El timeout de lectura no protege de eso: mide el hueco **entre** bytes y el goteo de
keep-alive lo reinicia para siempre. Pasó el 2026-09-14 con DeepSeek, que tardaba 15 minutos en
devolver una respuesta vacía; cada corrida quedaba clavada ese rato sin un solo aviso.

Ahora cada llamada tiene un tope de reloj de pared (`LLMConfig.deadline_s`, 180 s). Cumplido el
tope se corta, se registra `llm.<proveedor>.request.deadline` y **no se reintenta con el mismo
proveedor** —no es un hipo de red, está sin responder—: la cadena de fallback sigue con el
siguiente (`memex-llm settings show` la muestra).

Esperar el tope una vez está bien; esperarlo en cada mensaje, no. Con el proveedor caído, 20
mensajes eran una hora de reloj muerto, así que hay además un **corta-circuito por proveedor**: dos
topes seguidos y queda cortado 10 minutos (`llm.breaker.open`). Mientras esté cortado sus llamadas
fallan de una, sin red y sin espera, y la cadena de fallback pasa directo al siguiente. Una sola
respuesta buena lo reabre (`llm.breaker.closed`). Es memoria del proceso, no de la base: cada
proceso aprende por su cuenta en segundos y un reinicio lo olvida.

El consumidor que tarde de verdad lleva su propio tope; `asuntos_assign` usa un modelo de
razonamiento y sus llamadas llegan a ~500 s legítimamente, así que tiene 900 s:

```
memex-llm settings set --consumer asuntos_assign --deadline 900
memex-llm settings set --consumer asuntos_assign --deadline 0    # volver al default
```

Código: `src/memex/processing/auto.py`, job en `src/memex/scheduler/jobs.py`, migraciones
`0107_auto_processing`, `0108_auto_processing_watermarks` y `0109_llm_deadline`, tests en
`tests/test_processing_auto.py`.
