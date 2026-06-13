# Experimento: codex en más apartados (summarizer / orchestrator / juez)

Repite el experimento del gate de relevancia (acuerdo / calidad / latencia vs el proveedor
actual) en otros consumidores de LLM, ahora que la construcción del cliente es pluggable por
consumidor (`llm_consumer_settings` + `build_llm_client`). **Este doc es la receta; las corridas
las hace el dueño** (codex = suscripción, sin costo en dólares pero ~8x de latencia; el baseline
DeepSeek PAGA → OK explícito por corrida).

Codex corre host-side **y dentro del contenedor** (binario en la imagen + sesión en
`/secrets/codex` + `MEMEX_CODEX_SANDBOX=danger-full-access`; el scheduler hereda ese env). No hay
que salir del despliegue.

## Cómo correr cada apartado

El flag `--provider codex` inyecta el cliente por corrida (override one-off, NO toca la config
persistida). Acepta `--codex-model` y `--model`.

```bash
# Summarizer
memex-summarize run --user 1 --limit 50 --provider codex
# Orchestrator (ruteo + extracción)
memex-extract run --user 1 --limit 50 --provider codex
# Combinado (resumen + extracción en una pasada)
memex-process run --user 1 --limit 50 --provider codex
# Juez/dedup de identidades (FASE 2)
memex-identidades merge --user 1 --limit 50 --provider codex
```

Baseline para comparar (DeepSeek, PAGA — pedir OK): la misma corrida sin `--provider` (usa la
config del consumer, DeepSeek por default). Comparar veredictos/resúmenes/items sobre el MISMO
conjunto de entrada.

Para dejar codex **persistente** en un apartado (sin flag, también en corridas del scheduler):

```bash
memex-llm settings set --consumer summarizer --provider codex --codex-model gpt-5.1
memex-llm settings show
```

## Qué medir

- **Parseable**: ¿la salida de codex entra en el parser del consumidor sin degradar? (ver abajo)
- **Latencia**: codex ~8x vs DeepSeek (es un agente, no una completion). Aceptable en lotes
  nocturnos, dudoso para el camino por-mensaje del dashboard.
- **Calidad/acuerdo**: ¿coincide el resultado con el de DeepSeek sobre la misma entrada?
- **Costo**: codex = $0 en `llm_calls` (la suscripción no factura por token) → /métricas queda
  ciego para esas llamadas. El proveedor que sirvió queda en `llm_calls.model` (`codex/...`).

## Resultados: summarizer (corrida 2026-06-12, muestra chica)

8 ventanas reales ya resumidas por DeepSeek (6 individuales + 2 batch de 3 mensajes), mismo
prompt del worker, codex modelo default vía suscripción, **sin tocar la DB** (script en
`experiments/codex_summarizer/`, gitignored; salidas crudas en `results.json`).

- **Completitud**: 8/8 OK, cero fallos de sesión/CLI.
- **Acuerdo factual**: sin contradicciones con el baseline en ningún caso (montos, fechas,
  conductores, juegos, descuentos, números de pedido coinciden). codex agrega detalle extra
  fiel al original (desgloses de tarifa, créditos, número de factura).
- **Estilo**: codex es más verboso — en los individuales cortos ~2-4x más largo, sobre todo
  por boilerplate de plantilla («correo automático, no responder», «disponible PDF, dejar
  propina») que DeepSeek omite. Mitigable con una línea de prompt si se adopta (los prompts
  no se tocaron en esta campaña).
- **Latencia**: min 17.2s / mediana 22.8s / max 27.2s por ventana (DeepSeek típico 2-5s).
  Un lote de 50 ventanas ≈ 19 min vs ~3 min.

**Veredicto**: codex SIRVE en el summarizer — salida texto plano (sin riesgo de parseo),
calidad factual a la par del baseline, costo $0 (suscripción). El precio es la latencia
(~8-10x): apto para lotes desatendidos (scheduler nocturno), no para el camino interactivo
del dashboard. El boilerplate extra es el único desvío de calidad observado.

## Tolerancia a fences/prosa: RESUELTA en el cliente

codex (y anthropic) devuelven **JSON solo por prompt** y a veces lo envuelven en fences
` ```json ` o prosa. El saneo vive en el **cliente del proveedor** (donde se encapsulan las
rarezas de cada vendor): cuando el caller pide `response_format="json_object"`, `CodexClient` y
`AnthropicClient` pasan la salida por `memex.llm._json.normalize_json_output`, que extrae el
JSON **solo si el candidato parsea** — si nada parsea, el contenido pasa crudo y el parser del
worker degrada a su fallback seguro, como siempre. DeepSeek no lo necesita (modo JSON nativo).
Cada normalización queda auditada (`llm.codex.json_normalized` / `llm.anthropic.json_normalized`
→ /logs).

Estado por apartado (tests en `tests/test_codex_parser_tolerance.py` + `tests/llm/`):

| Apartado | Parser | Con codex |
|---|---|---|
| **summarizer** | texto plano (sin `json.loads`, sin saneo) | tolerante total — toma el texto tal cual |
| **orchestrator** (ruteo y extracción) | `json.loads` directo | recibe JSON ya saneado por el cliente; si aun así no parsea, degrada seguro (ruteo→todos, ventana sin items) |
| **jueces identidades/calendar/finance** | `json.loads` directo | ídem; degradación segura = no fusionar (sesgo a coexistir) |
| **gate de relevancia** | `parse_gate_verdicts` con `_strip_fences` propio | doble defensa (su strip quedó redundante e inofensivo) |
