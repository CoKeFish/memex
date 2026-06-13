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

## Veredicto por apartado (tolerancia de parsers — verificado en código)

codex devuelve **JSON solo por prompt** y a veces lo envuelve en fences ` ```json `. Hallazgo
(tests en `tests/test_codex_parser_tolerance.py`):

| Apartado | Parser | Ante fences de codex |
|---|---|---|
| **summarizer** | texto plano (sin `json.loads`) | **tolerante total** — toma el texto tal cual |
| **orchestrator** (ruteo) | `routing.parse_routing` → `json.loads` directo | degrada SEGURO → `None` → "todos los candidatos" (sobre-rutea, no crashea) |
| **orchestrator** (extracción) | `grouping`/`contract` → `json.loads` directo | degrada SEGURO → ventana sin items (no crashea) |
| **juez identidades/calendar/finance** | `_parse_decision` → `json.loads` directo | degrada SEGURO → `same=False` (no fusiona; sesgo a coexistir) |
| **gate de relevancia** | `parse_gate_verdicts` con `_strip_fences` | **tolerante real** — parsea con o sin fences |

Conclusión: ningún parser CRASHEA con codex, pero **solo el gate strip-ea fences**. Si en la
corrida real codex emite fences (los modelos a veces lo hacen), los consumidores JSON degradan a
su fallback seguro — lo que se vería como "codex no sirve acá" cuando en realidad es un hueco del
parser, no del modelo. El summarizer (texto plano) no tiene ese riesgo.

## Recomendación (follow-up, no incluido en esta campaña)

Si el dueño confirma que codex emite fences en estos prompts y quiere robustez (no solo
degradación), promover el helper `memex.relevance.prompts._strip_fences` a un util compartido de
`memex.llm` y aplicarlo en los parsers JSON (`parse_routing`, `grouping`/`contract`,
`_parse_decision`×3, `clusters`, `resolve`, `hierarchy`). Es backward-compatible: sobre el JSON
desnudo de DeepSeek (`response_format=json_object`) es un no-op. Decisión del dueño — no se tocó
acá para respetar el alcance "solo cableo".
