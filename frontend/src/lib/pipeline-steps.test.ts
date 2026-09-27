import { describe, expect, it } from "vitest"
import { buildPipelineSteps, nextStep } from "./pipeline-steps"
import type { InboxRow } from "@/types/domain"

function row(over: Partial<InboxRow> = {}): InboxRow {
  return {
    id: 1,
    sourceId: 1,
    externalId: "x",
    occurredAt: "2026-09-01T10:00:00Z",
    receivedAt: "2026-09-01T10:01:00Z",
    payload: {} as InboxRow["payload"],
    processedAt: null,
    processError: null,
    attempts: 0,
    classification: null,
    relevanceVerdict: null,
    relevance: null,
    extraction: null,
    llm: null,
    media: [],
    failures: [],
    ...over,
  }
}

const states = (r: InboxRow) => Object.fromEntries(buildPipelineSteps(r).map((s) => [s.key, s.state]))

const verdict = (v: "relevant" | "not_relevant" | "insufficient") => ({
  verdict: v,
  method: "llm",
  reason: "porque sí",
  mode: "per_window",
  model: "m",
  createdAt: "2026-09-01T11:00:00Z",
})

const extraction = (ids = 0) => ({
  done: true,
  modules: ["finance", "identidades"],
  finance: [{ amount: 1 }],
  calendar: [],
  hackathones: [],
  identidades: Array.from({ length: ids }, () => ({ mentioned_name: "x" })),
})

describe("buildPipelineSteps — orden y estados", () => {
  it("mensaje recién ingerido: solo ingesta hecha, la siguiente es clasificar", () => {
    const s = buildPipelineSteps(row())
    expect(s.map((x) => x.key)).toEqual([
      "ingest",
      "classify",
      "relevance",
      "route",
      "extract",
      "resolve",
      "graph",
    ])
    expect(states(row())).toMatchObject({ ingest: "done", classify: "next", relevance: "pending" })
    expect(nextStep(s)?.key).toBe("classify")
  })

  it("la etapa de adjuntos solo aparece si el mensaje los tiene, y va antes de clasificar", () => {
    const r = row({ payload: { attachments: [{ filename: "a.pdf" }] } as InboxRow["payload"] })
    const keys = buildPipelineSteps(r).map((x) => x.key)
    expect(keys.indexOf("media")).toBe(1)
    expect(states(r).media).toBe("next") // declarado y sin bajar → es lo primero que falta
  })

  it("no relevante: lo que sigue no aplica y no hay «siguiente»", () => {
    const r = row({
      classification: { tier: "blacklist", metadata: { rule: "list_unsubscribe" } },
      relevanceVerdict: verdict("not_relevant") as InboxRow["relevanceVerdict"],
    })
    expect(states(r)).toMatchObject({
      classify: "done",
      relevance: "done",
      route: "skipped",
      extract: "skipped",
      resolve: "skipped",
      graph: "skipped",
    })
    expect(nextStep(buildPipelineSteps(r))).toBeNull()
  })

  it("relevancia en duda: lo que sigue espera decisión", () => {
    const r = row({
      classification: { tier: "individual", metadata: {} },
      relevanceVerdict: verdict("insufficient") as InboxRow["relevanceVerdict"],
    })
    expect(states(r)).toMatchObject({ route: "blocked", extract: "blocked" })
  })

  it("la marca manual manda sobre el veredicto del gate", () => {
    const r = row({
      classification: { tier: "blacklist", metadata: {} },
      relevanceVerdict: verdict("not_relevant") as InboxRow["relevanceVerdict"],
      relevance: { isRelevant: true, reason: null, createdAt: null, updatedAt: "2026-09-01T12:00:00Z" },
    })
    const rel = buildPipelineSteps(r).find((s) => s.key === "relevance")!
    expect(rel.fact).toBe("manual · relevante")
    expect(states(r).extract).toBe("next")
  })

  it("procesado completo: ruteo, extracción y resolver hechos; el grafo queda «sin traza»", () => {
    const r = row({
      classification: { tier: "batch", metadata: {} },
      relevanceVerdict: verdict("relevant") as InboxRow["relevanceVerdict"],
      extraction: extraction(2),
      llm: {
        calls: 3,
        costUsd: 0.001,
        promptTokens: 10,
        completionTokens: 5,
        items: [
          { purpose: "module_route", model: "m", promptTokens: 1, completionTokens: 1, costUsd: 0.0001, latencyMs: 1, status: "ok", metadata: { chosen: ["finance", "identidades"] } },
          { purpose: "extract_grouped", model: "m", promptTokens: 1, completionTokens: 1, costUsd: 0.0003, latencyMs: 1, status: "ok" },
          { purpose: "identidades_resolve", model: "m", promptTokens: 1, completionTokens: 1, costUsd: 0.0002, latencyMs: 1, status: "ok", metadata: { merged: 1, affiliated: 2, linked: 0 } },
        ],
      },
    })
    const s = buildPipelineSteps(r)
    expect(states(r)).toMatchObject({ route: "done", extract: "done", resolve: "done", graph: "unknown" })
    expect(s.find((x) => x.key === "route")?.fact).toBe("finance, identidades")
    expect(s.find((x) => x.key === "extract")?.fact).toBe("3 datos")
    expect(s.find((x) => x.key === "resolve")?.fact).toBe("1 fusión · 2 vínculos")
    expect(s.find((x) => x.key === "extract")?.costUsd).toBeCloseTo(0.0003)
    expect(nextStep(s)).toBeNull()
  })

  it("extraída a mano sin gate: relevancia queda omitida, no «siguiente»", () => {
    const r = row({
      classification: { tier: "batch", metadata: {} },
      relevanceVerdict: null,
      extraction: extraction(1),
    })
    const s = buildPipelineSteps(r)
    expect(s.find((x) => x.key === "relevance")).toMatchObject({ state: "skipped", fact: "sin veredicto" })
    expect(states(r)).toMatchObject({ route: "done", extract: "done" })
    expect(nextStep(s)?.key).toBe("resolve") // lo único que falta de verdad
  })

  it("extraída sin identidades: el resolver no aplica", () => {
    const r = row({
      classification: { tier: "batch", metadata: {} },
      relevanceVerdict: verdict("relevant") as InboxRow["relevanceVerdict"],
      extraction: extraction(0),
    })
    expect(states(r).resolve).toBe("skipped")
  })

  it("una falla registrada se muestra como «falló» con el error", () => {
    const r = row({
      classification: { tier: "batch", metadata: {} },
      relevanceVerdict: verdict("relevant") as InboxRow["relevanceVerdict"],
      failures: [
        { stage: "extract", status: "failing", attempts: 1, lastError: "FK org_id=9", updatedAt: "2026-09-01T12:00:00Z" },
      ],
    })
    const ext = buildPipelineSteps(r).find((s) => s.key === "extract")!
    expect(ext.state).toBe("failed")
    expect(ext.detail).toBe("FK org_id=9")
    expect(nextStep(buildPipelineSteps(r))).toBeNull() // no hay «siguiente» mientras haya que arreglar
  })
})
