# Investigación: contexto web para entidades (org/producto)

> Investigación **read-only** (2026-06-16/17). Decide **CÓMO** encarar la capacidad futura
> «dado un org/producto nuevo sin contexto, traer un perfil corto de la web». **NO implementa la
> feature.** Alcance de la futura feature: **SOLO orgs/productos, nunca personas**
> (privacidad/desperdicio). Método: lectura de código + docs + `ctx7` (un workflow de 7 agentes) y
> **probes reales** de Firecrawl y codex.

## Resumen ejecutivo

- **A — ¿Módulo o servicio?** → **Servicio**, no módulo de dominio. El contrato `InterestModule`
  exige consumir record-kinds y atribuir cada dato a un `inbox_id`; un enriquecimiento se dispara
  desde un *vértice existente*, no desde un mensaje. Los vértices org/producto los **posee
  identidades** → el servicio los **anota**. **No es copia de geo**: se toma de geo solo el *seam*
  de proveedor + caché/procedencia; la forma exacta depende del alcance (ver A).
- **B — Técnica.** v1 = **codex como proveedor** del servicio: en una sola llamada hace búsqueda +
  selección de fuentes + síntesis + JSON conforme, a **costo $0** (suscripción). Detrás de un
  **seam de proveedor** con **Firecrawl como fallback** (predecible, mockeable, control de costo).
  Recomendación re-pesada con las prioridades del dueño: **el costo manda, la latencia es
  irrelevante, y determinismo/mockabilidad/costo-visible son deseables, no requisitos.**

---

## Pregunta A — Módulo de dominio vs servicio

### Por qué NO es un módulo (`InterestModule`)
El contrato de módulo (`src/memex/modules/contract.py`) obliga dos cosas que el enriquecimiento web
no cumple:
1. **`consumes_kinds`** + ruteo por el *kind de la ventana del inbox* (`routing.py`). El
   enriquecimiento se dispara por un vértice, no por un mensaje.
2. **Atribución por-mensaje**: `ExtractionItem.source_inbox_ids` es obligatorio y `validate_item`
   **descarta** lo que no caiga en el lote. El contexto web no nace de un `inbox_id`.

Además los vértices `org`/`producto` los **posee identidades** (`relations/vertices.py`,
`NODE_SOURCES` slugs `identidades:org`/`producto`). Un enriquecimiento web los **anotaría** → choca
con «un dueño por TIPO».

### Precedentes en el repo
- **Sync de proveedores de identidades** (Google/Outlook): entra *directo* a `mod_identidades`
  «sin pasar por el motor ni la IA», por un job del scheduler. **Es el molde** de un enriquecimiento
  no-extractivo dentro de un dominio.
- **geo**: subsistema de «resolver/anotar a demanda» (Protocol de provider + registry + resultados
  tipados + caché/procedencia + dual-mock); los dominios lo referencian por FK, geo no los conoce.
- **bienestar**: dominio con vértices propios pero **NO** `InterestModule` (lo escribe un CLI
  determinista) → «dominio con vértices» ≠ «módulo de extracción».

### Recomendación A — servicio best-fit (no geo-by-default)
**Servicio nuevo** (gateado OFF, on-demand/lote, nunca Source de ingesta). La **forma exacta** la
decide una pregunta de alcance:

- **Solo-identidad** (el contexto web es propiedad de org/producto y nada más) → lo más adecuado es
  el patrón **sync de identidades**: una vía de enriquecimiento más *dentro* del dominio
  identidades, con scaffolding mínimo. No hace falta el aparato completo de geo.
- **Transversal** (mañana finance=comercios, calendar=lugares también lo quieren) → **servicio
  standalone** con seam de proveedor + store de contexto/procedencia propio que los dominios
  referencian por FK, y que es además **tool tipada para Hermes**.

**De geo se toma** el seam de proveedor (Protocol + registry + resultados tipados frozen +
dual-mock). **NO se copia**: el store de 4 capas ni la clave fuerte `provider_place_id`. Decisión de
diseño pendiente: **la clave de identidad de entidad** para deduplicar el caché (dominio canónico /
Wikidata id / nombre normalizado), porque una búsqueda web parte de un nombre difuso, no de una
clave fuerte como geo.

> Decisión abierta para el dueño: **¿solo-identidad o transversal?** Define la arquitectura.

---

## Pregunta B — Técnica

### La duda exec→MCP/skills (resuelta por docs, `ctx7`)
| Agente vía exec | MCP | Skills | Web search |
|---|---|---|---|
| **codex** (`codex exec`) | **Sí**, configurable (`[mcp_servers]` en `config.toml` de `CODEX_HOME`; emite `McpToolCall` en exec) | **No** nativo (concepto de Claude; lo emulan toolkits de terceros sobre `AGENTS.md`) | **Sí, built-in** (`web_search_mode` = disabled / cached(default) / live; requiere provider OpenAI = se cumple) |
| **Claude headless** (`claude -p` / Agent SDK) | **Sí**, configurable | **Sí** (CLI `-p` por defecto, opt-out `--bare`; SDK off por defecto) | vía MCP/skill |

**Implicación:** sí — un agente vía exec puede orquestar con un MCP de Firecrawl o una skill de
búsqueda. La red en codex es un toggle separado del FS; el `web_search` built-in es server-side
(provider hosted), no depende de red local.

### Matriz (re-pesada por prioridades del dueño)
| Criterio | **codex exec** (suscripción) | **Firecrawl / search-API + LLM atómico** |
|---|---|---|
| **Costo** ⭐ | **$0 marginal** (suscripción) | crédito/fetch (Free 1000cr; search 2cr/10res, scrape 1cr/pág) |
| Latencia (irrelevante) | ~8x (46-68s/perfil, medido) | bajo (3-8s, medido) |
| Calidad del perfil | **alta**, **auto-selecciona fuentes** reales/diversas | **alta SOLO si se apunta a la URL canónica**; top-1 ciego falla |
| Fuentes/procedencia | reales y diversas, en una llamada | las que vos elijas scrapear |
| Formato | **JSON forzado** vía `--output-schema` | JSON vía `formats:[{type:json,schema}]` |
| MCP / skills | MCP sí · skills no | N/A (cliente) |
| Determinismo (deseable) | bajo en contenido; formato forzado por schema | extract también es LLM (contenido no-det); fetch cacheable |
| Testabilidad/mock (deseable) | difícil (stub del binario) | **excelente** (`respx`, como geo) |
| Tokens / contabilidad | **tokens SÍ** (`--json` `turn.completed.usage`); USD no (suscripción) | tokens del LLM de resumen propios; créditos visibles |
| Operación/auth | sesión codex (frágil: ver incidente); **login dedicado** | API key (Doppler) |

### Recomendación B (v1)
**codex como proveedor del servicio**, detrás de un **seam de proveedor**, con **Firecrawl como
fallback**. Razón: bajo costo-primero + latencia-irrelevante, codex resuelve búsqueda + selección de
fuentes + síntesis + JSON en una llamada a $0, sin la lógica de «qué URL» que el camino Firecrawl
obliga a construir y mantener. Firecrawl queda como fallback predecible/mockeable y para control de
costo determinista. El seam permite cambiar sin tocar a los consumidores.

---

## Evidencia empírica (probes reales, 2026-06-17)

### Firecrawl (API v2, key del dueño) — `experiments/webcontext_probe/fc_probe.py`, `fc_canonical.py`
- `/search` (~1-3s) y `/scrape` con `formats:[{type:json, schema}]` (~3-8s) **funcionan**.
- **Calidad alta apuntando a fuente canónica**: Rappi (Wikipedia) → multinacional colombiana, 2015,
  fundadores, unicornio, 9 países, $5.2B Serie G 2021. Notion (Wikipedia) → app productividad,
  Notion Labs, 2016, $10B 2021.
- **Debilidad = selección de URL**: con top-1 ciego, Rappi devolvió *RappiCuenta Empresas* (producto
  de pagos), **no** la empresa. El pipeline correcto es **search → elegir canónica → scrape-extract**.

### codex (contenedor, `web_search_mode=live`, `--output-schema`) — vía `docker exec memex-api`
- **Rappi** (68s, rc=0): superapp colombiana (delivery+ecommerce+fintech), ago-2015 Bogotá,
  Borrero/Villamarín/Mejía, YC W2016, +200k comercios/+300 ciudades.
  `sources: about.rappi.com/about-us, ycombinator.com/companies/rappi, play.google.com/...`
- **Notion** (46s, rc=0): app de productividad, Notion Labs, lanzada 2016, web/desktop/móvil, SF.
- **codex auto-selecciona fuentes** reales y diversas (sin lógica de URL nuestra); `--output-schema`
  garantiza el JSON conforme.
- **Tokens confirmados** (cierra la corrección de costos): `turn.completed.usage =
  {input_tokens:12416, cached_input_tokens:11136, output_tokens:49, reasoning_output_tokens:42}`;
  además stderr imprime `tokens used N` (38.182 Rappi, 26.910 Notion).

### Incidente operativo (dato de la matriz)
La `auth.json` montada (copiada del host) **murió** mid-probe: `refresh token already used → 401`.
Causa: linaje de refresh-token compartido con el host (codex rota tokens de un solo uso). **Fix
verificado**: login **dedicado** apuntando al `CODEX_HOME` del contenedor
(`CODEX_HOME=<repo>/secrets/codex codex login`), **no** copiar el `auth.json` del host. Tras el
login dedicado, todas las corridas dieron rc=0.

### Head-to-head (Rappi, caso discriminante)
| | Resultado |
|---|---|
| FC top-1 ciego | ❌ entidad equivocada (RappiCuenta, pagos) |
| FC fuente canónica | ✅ correcto — requiere elegir la URL buena |
| **codex web_search** | ✅ correcto + auto-selección de fuentes, una llamada, JSON, $0 |

---

## Corrección de costos (acordada, independiente de la feature)
`src/memex/llm/codex.py` arma hoy `LLMUsage(0,0,0)` / `cost_usd=0`. Pero `codex exec --json` emite
`turn.completed.usage`. **Cambio**: correr con `--json`, parsear los tokens reales (input/cached/
output/reasoning) → poblar `LLMUsage`; el USD queda 0 (suscripción) o notional desde
`MEMEX_LLM_PRICING`. Toca `codex.py` + su test. (Corrige el caveat «sin contabilidad de tokens» del
docstring, que sólo aplica al USD.)

## Próximos pasos
1. Confirmar **alcance A** (solo-identidad vs transversal) → fija la arquitectura del servicio.
2. Implementar v1: servicio con **seam de proveedor**, **provider codex** (primario) + **Firecrawl**
   (fallback), store de contexto + procedencia; gateado OFF; CLI/endpoint (tool tipada).
3. **Evaluar desempeño** en lote (calidad de perfiles, cobertura de fuentes).
4. Hacer la **corrección de costos** de codex.
5. **Operación**: documentar el login dedicado de codex para el contenedor (runbook) y **rotar la
   key de Firecrawl** usada en el probe (quedó en chat/logs).
