# Notificaciones: cola, daemon, productores y canales

memex tiene una sola cola de avisos (`notifications`, migración 0075) y, desde la 0097, un daemon
que la **produce** (qué avisar) y la **entrega** (por dónde). El dashboard sigue leyendo la misma
cola en `/notificaciones` y en la campana; Telegram es el primer canal de empuje.

## Dos audiencias

| `audience` | Qué es | Ejemplos |
|---|---|---|
| `personal` | La vida del usuario | resumen matutino, «en 60 min: clase», tarea que vence hoy, plazo de un asunto, pago fallido |
| `sistema` | memex por dentro | cuenta con credenciales caídas, ingesta fallida, worker colgado o en error, saldo LLM agotado, backlog de revisión |

Cada canal se suscribe por audiencia con una severidad mínima (`info` < `alta` < `critica`).
Default de un canal nuevo: todo lo personal y solo lo crítico del sistema.

## Piezas (`src/memex/notifications/`)

| Archivo | Rol |
|---|---|
| `client.py` | `Notification` (con `audience`) y el seam `Notifier` que usan los emisores (transporte sigue emitiendo por acá). |
| `store.py` | Cola (`enqueue` idempotente por `dedup_key`, leído/descartado pegajoso), canales y entregas. |
| `settings.py` | Env `MEMEX_NOTIFICATIONS_*` (bootstrap) + DB: `notification_settings` (daemon prendido, zona horaria) y `notification_producers` (por productor: activo, `render`, `config`). |
| `producers/` | Un módulo por productor. `ProducerSpec` declara audiencia, defaults y `produce(ctx) -> [Draft]`. Registro en `producers/__init__.py`. |
| `render.py` | `format_message` (texto del canal) y `LLMWriter` (redacta a partir de `Draft.facts`; consumidor LLM `notifications`; costo en `llm_calls` con propósito `notifications_render`). |
| `channels/` | `Channel.send(target, text, notification)`. `telegram` (mismo bot del asistente; solo envía) y `log`. Registro en `channels/__init__.py`. |
| `service.py` | `run_cycle(user_id)`: productores → cola → entregas. Idempotente; `dry_run` no escribe ni llama al modelo. |
| `daemon.py` | Loop cada `tick_seconds` (30 s) si `daemon_enabled`. Ciclos con actividad dejan fila en `worker_runs` (job `notifications`). |
| `cli.py` | `memex-notifications …` (abajo). |

`memex.core.alerts.compute_alerts` es el cálculo de alertas de observabilidad que antes vivía solo
en `GET /stats/alerts`; ahora lo comparten el router y el productor `sistema`.

## Productores

| Nombre | Audiencia | Default | Redacción | Config (`--set clave=valor`) |
|---|---|---|---|---|
| `resumen_diario` | personal | activo | `llm` (plantilla si falla) | `hora=07:00`, `dias_tareas=7`, `dias_asuntos=7`, `dia_panorama=0` (0 = lunes … 6 = domingo) |
| `recordatorio_evento` | personal | activo | plantilla | `anticipacion_min=60`, `compromisos=confirmado,provisional` |
| `tareas_del_dia` | personal | activo | plantilla | `hora=08:00` |
| `asuntos_plazo` | personal | activo | plantilla | `hora=08:00`, `dias=3` (un aviso por asunto abierto con `vence` en la ventana) |
| `renovaciones` | personal | activo | plantilla | `dias_anticipacion=3` (un aviso por suscripción y por renovación, a tiempo para cancelarla) |
| `pagos_pendientes` | personal | activo | plantilla | `hora=08:00`, `edad_minima_dias=0`, `dias_vigencia=14` (un aviso por pago fallido/pendiente sin regularizar) |
| `verificar_cuentas` | sistema | activo | plantilla | `cada_horas=6` (prueba las credenciales de cada cuenta contra el proveedor; no emite: los fallos salen por `sistema`) |
| `sistema` | sistema | activo | plantilla | `horas_vigencia=48` (incluye «cuenta con error», crítica: un aviso por cuenta y por día mientras siga caída) |

**Cadencia del resumen (2026-09-26).** El dueño: «hay cosas que no me interesa recordar todos los
días, como lo de la condonación». Antes todo asunto de prioridad alta salía a diario, idéntico, y
un pago fallido cada mañana. Ahora un asunto entra al resumen solo con MOTIVO (vence dentro de
`dias_asuntos`, o hubo novedad: cambió su estado en las últimas 24 h) y la línea lo dice
(`· vence 12/09`, `· novedad: …`). Lo viejo vuelve con cadencia, no a diario: una tarea vencida y
un pago fallido salen los días 0-3, 7, 14, 21, 30 y luego cada 30 (`CADENCIA_DIAS`). Un día por
semana (`dia_panorama`, lunes por default) va el **panorama**: una línea con todos los asuntos
abiertos en prioridad alta y todo lo vencido, para que nada quede olvidado del todo.

Reglas comunes: cada borrador lleva una `dedup_key` (`resumen:<fecha>`, `evento:<id>:<fecha>`,
`tarea:<id>:<fecha>`, `sistema:<alerta>:<instante>`); si ya está en la cola se descarta **antes**
de redactar, así el modelo se paga una vez por aviso. Las horas se comparan en la zona del usuario
(`notification_settings.timezone`, default America/Bogota). `render=llm` solo aplica a productores
con `llm_allowed` (hoy el resumen); el modelo recibe los hechos estructurados y una consigna corta,
y no puede inventar datos.

## Entrega

`pending_deliveries` devuelve los pares (aviso, canal) que faltan: aviso activo, **no leído ni
descartado** en el dashboard, creado después del alta del canal, cuya audiencia el canal suscribe,
sin entrega `enviada` (las `fallida` reintentan hasta `max_attempts`, con `retry_after_s` entre
intentos). En Python se aplican la severidad mínima y las **horas de silencio** del canal (en
silencio solo pasa lo `critica`; el resto espera). Entregar no marca el aviso como leído.

## CLI

```
memex-notifications status
memex-notifications enable | disable                      # el daemon relee la DB cada tick
memex-notifications daemon start                          # bloquea; apagado por default
memex-notifications run [--dry-run] [--producer N] [--json]
memex-notifications producers show
memex-notifications producers set resumen_diario --set hora=06:30 --render plantilla
memex-notifications producers set recordatorio_evento --set anticipacion_min=30
memex-notifications producers set tareas_del_dia --disable
memex-notifications channels add telegram --target <chat_id> [--personal info] [--sistema critica] [--quiet 22:00-07:00]
memex-notifications channels list | enable <id> | disable <id> | rm <id> | test <id>
memex-notifications deliveries [--limit N]
memex-notifications settings --timezone America/Bogota
memex-notifications purge
```

`chat_id` de Telegram: en un chat 1:1 es el id del usuario (el mismo de
`MEMEX_ASSISTANT_ALLOWED_USER_IDS`). El token sale de `MEMEX_NOTIFICATIONS_TELEGRAM_BOT_TOKEN` o,
si no está, de `MEMEX_ASSISTANT_TELEGRAM_BOT_TOKEN` (es el mismo bot; enviar no choca con el
polling del asistente). `MEMEX_NOTIFICATIONS_DASHBOARD_URL` convierte el `deep_link` en enlace.

## API y panel

`GET/PATCH /notifications/settings` (daemon prendido, zona horaria; devuelve productores con su
config efectiva, canales y canales disponibles), `PATCH /notifications/producers/{name}` (activo,
render, config validada contra los defaults), `GET/POST /notifications/channels`,
`PATCH/DELETE /notifications/channels/{id}`, `POST /notifications/channels/{id}/test`,
`GET /notifications/deliveries`, `POST /notifications/run` (`dry_run` por default). La página
`/notificaciones` muestra la cola (con filtro por audiencia) y, debajo, el panel: productores con
switch, redacción y tiempos editables; canales con suscripciones, silencio, probar y quitar;
últimas entregas; «Simular ciclo» / «Correr ciclo ahora». El `channels test` y `run` desde el API
necesitan el token del bot en el env del contenedor `api` (`MEMEX_ASSISTANT_TELEGRAM_BOT_TOKEN`).

## Correr

- Host: `doppler run -p shared -c dev -- uv run memex-notifications daemon start` (desde la raíz
  de main: necesita el `.env`).
- Compose: `docker compose --profile notifications up -d notifications` (profile, apagado por
  default como el resto de daemons).
- Alternativa sin proceso nuevo: el job `notifications` del scheduler (`PT1M`) corre el mismo ciclo.
  Correr ambos es seguro (todo es idempotente), pero basta uno.

## Agregar un productor o un canal

Productor: módulo en `producers/` con `SPEC = ProducerSpec(...)` y una línea en `_registry()`.
Lee datos con `ctx.conn` (no abre conexiones), devuelve `Draft`s con plantilla + `facts`; si vale
la pena redactarlo con modelo, `llm_brief` y `llm_allowed=True`. Canal: clase con `name`, `send`,
`aclose` en `channels/` y una fábrica en `_FACTORIES`. Ninguno toca la cola ni las entregas.

## Pendiente

El agente de Telegram leyendo o silenciando avisos (`memex notificaciones …`), y canales nuevos
(push web, correo) detrás del mismo seam.
