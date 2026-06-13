import { describe, expect, it } from "vitest"
import {
  consumerPatchToBody,
  effectiveConfig,
  effectiveSource,
  LLM_DEFAULT_CONSUMER,
  LLM_OPERATIONS,
  type LlmConsumerConfig,
  toConsumerConfig,
} from "./llm"

/** Espejo de LLM_CONSUMERS (src/memex/llm/settings.py): el panel debe cubrir EXACTAMENTE estas
 * claves. Si el backend agrega/renombra un consumer, este test obliga a reflejarlo en la UI. */
const BACKEND_CONSUMERS = [
  "default",
  "summarizer",
  "orchestrator",
  "process",
  "calendar_dedup",
  "calendar_merge",
  "finance_dedup",
  "identidades_dedup",
  "identidades_cooccurrence",
  "identidades_hierarchy",
  "relations_confirm",
  "relations_clusters",
  "quality_judge",
]

function cfg(over: Partial<LlmConsumerConfig> = {}): LlmConsumerConfig {
  return { consumer: "summarizer", provider: "deepseek", model: null, codexModel: null, fallback: [], ...over }
}

describe("LLM_OPERATIONS · cobertura del registry", () => {
  it("cubre exactamente los consumers del backend (default + cada paso, sin huérfanos ni typos)", () => {
    const keys = [LLM_DEFAULT_CONSUMER, ...LLM_OPERATIONS.flatMap((op) => op.steps.map((s) => s.key))]
    expect(new Set(keys)).toEqual(new Set(BACKEND_CONSUMERS))
    expect(keys.length).toBe(BACKEND_CONSUMERS.length) // sin claves repetidas
  })
})

describe("toConsumerConfig · snake → camel", () => {
  it("mapea codex_model → codexModel y normaliza fallback ausente a []", () => {
    expect(
      toConsumerConfig({
        consumer: "summarizer",
        provider: "codex",
        model: null,
        codex_model: "gpt-5.1",
        fallback: ["deepseek"],
      }),
    ).toEqual({
      consumer: "summarizer",
      provider: "codex",
      model: null,
      codexModel: "gpt-5.1",
      fallback: ["deepseek"],
    })
  })
})

describe("consumerPatchToBody · camel → snake (parcial)", () => {
  it("solo serializa los campos presentes, codexModel → codex_model", () => {
    expect(consumerPatchToBody({ provider: "anthropic", model: "claude-opus-4-8" })).toEqual({
      provider: "anthropic",
      model: "claude-opus-4-8",
    })
    expect(consumerPatchToBody({ codexModel: "gpt-5.1" })).toEqual({ codex_model: "gpt-5.1" })
    expect(consumerPatchToBody({ model: "" })).toEqual({ model: "" }) // limpia el override
    expect(consumerPatchToBody({})).toEqual({})
  })
})

describe("effectiveSource / effectiveConfig · procedencia del valor", () => {
  it("fila propia → own", () => {
    const configured = [cfg({ consumer: "summarizer", provider: "anthropic" })]
    expect(effectiveSource("summarizer", configured)).toBe("own")
    expect(effectiveConfig("summarizer", configured)?.provider).toBe("anthropic")
  })

  it("sin fila propia pero con default → default", () => {
    const configured = [cfg({ consumer: "default", provider: "codex", codexModel: "gpt-5.1" })]
    expect(effectiveSource("summarizer", configured)).toBe("default")
    expect(effectiveConfig("summarizer", configured)?.consumer).toBe("default")
  })

  it("sin fila propia ni default → hardcode (DeepSeek)", () => {
    expect(effectiveSource("summarizer", [])).toBe("hardcode")
    expect(effectiveConfig("summarizer", [])).toBeNull()
  })
})
