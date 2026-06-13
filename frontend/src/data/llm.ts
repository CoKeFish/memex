// Selección de proveedor+modelo LLM por consumidor (registry general) contra la API real
// (/llm/consumers). Espejo de data/relevance.ts: funciones fetch/patch + transform snake↔camel.
// La fábrica `build_llm_client` (backend) lee `llm_consumer_settings`; este módulo expone esa
// config para el panel de /procesamiento. NO dispara LLM: solo configura qué cliente construirá
// cada consumer cuando corra. El gate de relevancia y el OCR usan sistemas aparte (ver el panel).

import { apiGet, apiPatch } from "@/lib/api"

export type LlmProvider = "deepseek" | "anthropic" | "codex"

/** Config resuelta de UN consumer (espeja LLMConsumerConfig del backend). `model`/`codexModel`
 * null = el default del proveedor; `fallback` = cadena ORDENADA de proveedores extra. */
export interface LlmConsumerConfig {
  consumer: string
  provider: LlmProvider
  model: string | null
  codexModel: string | null
  fallback: LlmProvider[]
}

export interface LlmConsumers {
  /** Claves válidas (LLM_CONSUMERS del backend). */
  consumers: string[]
  /** Proveedores válidos (LLM_PROVIDERS). */
  providers: LlmProvider[]
  /** Solo las filas que el usuario ya configuró; las ausentes usan `default` o el hardcode DeepSeek. */
  configured: LlmConsumerConfig[]
}

/** Patch parcial: solo los campos presentes se aplican. `model:""`/`codexModel:""` LIMPIAN el
 * override (vuelven al default del proveedor); `fallback:[]` borra la cadena. */
export interface LlmConsumerPatch {
  provider?: LlmProvider
  model?: string
  codexModel?: string
  fallback?: LlmProvider[]
}

// ---- API rows (snake_case) ----------------------------------------------------------------------

interface LlmConsumerConfigApi {
  consumer: string
  provider: LlmProvider
  model: string | null
  codex_model: string | null
  fallback: LlmProvider[]
}
interface LlmConsumersApi {
  consumers: string[]
  providers: LlmProvider[]
  configured: LlmConsumerConfigApi[]
}

// ---- transforms (puros, exportados para test) ---------------------------------------------------

export function toConsumerConfig(c: LlmConsumerConfigApi): LlmConsumerConfig {
  return {
    consumer: c.consumer,
    provider: c.provider,
    model: c.model,
    codexModel: c.codex_model,
    fallback: c.fallback ?? [],
  }
}

/** Cuerpo snake_case del PATCH: solo los campos presentes en el patch (upsert parcial del backend). */
export function consumerPatchToBody(patch: LlmConsumerPatch): Record<string, unknown> {
  const body: Record<string, unknown> = {}
  if (patch.provider !== undefined) body.provider = patch.provider
  if (patch.model !== undefined) body.model = patch.model
  if (patch.codexModel !== undefined) body.codex_model = patch.codexModel
  if (patch.fallback !== undefined) body.fallback = patch.fallback
  return body
}

// ---- API ----------------------------------------------------------------------------------------

/** Claves + proveedores + filas configuradas — GET /llm/consumers. */
export async function fetchLlmConsumers(): Promise<LlmConsumers> {
  const r = await apiGet<LlmConsumersApi>("/llm/consumers")
  return {
    consumers: r.consumers,
    providers: r.providers,
    configured: r.configured.map(toConsumerConfig),
  }
}

/** Upsert parcial de la fila del consumer — PATCH /llm/consumers/{consumer}. */
export async function patchLlmConsumer(
  consumer: string,
  patch: LlmConsumerPatch,
): Promise<LlmConsumerConfig> {
  return toConsumerConfig(
    await apiPatch<LlmConsumerConfigApi>(`/llm/consumers/${consumer}`, consumerPatchToBody(patch)),
  )
}

// ---- Catálogo: operaciones agrupadas ------------------------------------------------------------

/** Un paso (consumer del registry) dentro de una operación. */
export interface LlmStep {
  /** Clave del consumer; DEBE existir en LLM_CONSUMERS (lo verifica el test de cobertura). */
  key: string
  label: string
}

/** Una operación de procesamiento de cara al dueño y los consumers que la componen. 1 paso =
 * fila inline; >1 = sub-filas. El orden y las etiquetas son los que ve el dueño. */
export interface LlmOperation {
  label: string
  hint?: string
  steps: LlmStep[]
}

/** El consumer comodín: el default del sistema cuando una operación no tiene fila propia. Se
 * renderiza aparte como «Global (default)», no como una operación. */
export const LLM_DEFAULT_CONSUMER = "default"

export const LLM_OPERATIONS: LlmOperation[] = [
  { label: "Resumen", hint: "resumen de mensajes", steps: [{ key: "summarizer", label: "resumen" }] },
  {
    label: "Extracción",
    hint: "extracción de hechos a los módulos",
    steps: [{ key: "orchestrator", label: "extracción" }],
  },
  {
    label: "Procesamiento combinado",
    hint: "resumen + extracción en una sola llamada (CLI memex-modules)",
    steps: [{ key: "process", label: "combinado" }],
  },
  {
    label: "Calendario",
    steps: [
      { key: "calendar_dedup", label: "dedup (fase 2)" },
      { key: "calendar_merge", label: "fusión" },
    ],
  },
  { label: "Finanzas", steps: [{ key: "finance_dedup", label: "dedup (fase 2)" }] },
  {
    label: "Identidades",
    steps: [
      { key: "identidades_dedup", label: "dedup (fase 2)" },
      { key: "identidades_cooccurrence", label: "co-ocurrencia" },
      { key: "identidades_hierarchy", label: "jerarquía" },
    ],
  },
  {
    label: "Grafo · cúmulos",
    hint: "partidor LLM de cúmulos",
    steps: [{ key: "relations_clusters", label: "cúmulos" }],
  },
  {
    label: "Grafo · confirmar",
    hint: "confirmación de co-ocurrencia por-mensaje",
    steps: [{ key: "relations_confirm", label: "confirmar" }],
  },
  {
    label: "Juez de calidad",
    hint: "juez LLM de relevancia por remitente (a demanda)",
    steps: [{ key: "quality_judge", label: "juez" }],
  },
]

// ---- Catálogo: modelos por proveedor ------------------------------------------------------------

/** Modelos conocidos por proveedor (referencia: MODEL_PRICING en src/memex/llm/pricing.py + los
 * IDs Claude del entorno). La UI agrega «(default del proveedor)» (= null) y «custom…» (texto
 * libre) para modelos fuera de esta lista. */
export const MODELS_BY_PROVIDER: Record<LlmProvider, string[]> = {
  deepseek: ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash", "deepseek-v4-pro"],
  anthropic: ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"],
  codex: ["gpt-5.1"],
}

/** Modelos con tarifa en MODEL_PRICING; el resto computa costo desconocido en /métricas (codex no
 * mide tokens, así que sus modelos quedan fuera a propósito). */
export const PRICED_MODELS = new Set<string>([
  "deepseek-chat",
  "deepseek-reasoner",
  "deepseek-v4-flash",
  "deepseek-v4-flash-preview",
  "deepseek-v4-pro",
  "claude-opus-4-8",
])

// ---- Fuente efectiva (procedencia del valor) ----------------------------------------------------

export type EffectiveSource = "own" | "default" | "hardcode"

/** ¿De dónde sale el valor efectivo de un consumer? Fila propia → "own"; si no, existe fila
 * `default` → "default"; sin ninguna → "hardcode" (DeepSeek, el comportamiento previo a la tabla). */
export function effectiveSource(consumer: string, configured: LlmConsumerConfig[]): EffectiveSource {
  if (configured.some((c) => c.consumer === consumer)) return "own"
  if (configured.some((c) => c.consumer === LLM_DEFAULT_CONSUMER)) return "default"
  return "hardcode"
}

/** La config efectiva de un consumer: su fila propia, o la `default`, o null (→ hardcode DeepSeek,
 * que la UI muestra como provider=deepseek + modelo default). */
export function effectiveConfig(
  consumer: string,
  configured: LlmConsumerConfig[],
): LlmConsumerConfig | null {
  return (
    configured.find((c) => c.consumer === consumer) ??
    configured.find((c) => c.consumer === LLM_DEFAULT_CONSUMER) ??
    null
  )
}
