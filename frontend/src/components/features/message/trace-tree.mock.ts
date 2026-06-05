// Fixture del árbol de traza para iterar la UX de <TraceTree /> SIN backend (Fase A). Reproduce el
// ejemplo del dueño: identidades (extracción → menciones, una creada nueva, otra consolidada con
// desempate LLM) + finance (transacción → contraparte vía identidad + dedup LLM). Simula EXACTAMENTE
// lo que `read_trace` devolverá (camelCase + roll-up de costo ya calculado). Se borra en la Fase C.
//
// Patrón de DEDUP: cada comparación es un nodo `decision` que nombra la CONTRAPARTE ("vs «B» #id",
// linkeable vía `ref`) con score/decisión en `detail`; el desempate LLM cuelga BAJO esa comparación.
// La entidad A es el ancestro (implícita); cada hijo muestra contra quién se comparó.

import type { TraceNodeDto } from "@/types/domain"

/** Rellena los campos opcionales del nodo para no repetirlos en cada literal. */
function n(
  p: Partial<TraceNodeDto> & Pick<TraceNodeDto, "id" | "parentId" | "seq" | "kind" | "label">,
): TraceNodeDto {
  return {
    moduleSlug: null,
    status: null,
    ref: null,
    llmCallId: null,
    cost: { ownUsd: 0, subtreeUsd: 0, calls: 0 },
    detail: {},
    llm: null,
    ...p,
  }
}

const DS = "deepseek-chat"

export const MOCK_TRACE: TraceNodeDto[] = [
  n({ id: 1, parentId: null, seq: 0, kind: "root", label: "mensaje #1234", cost: { ownUsd: 0, subtreeUsd: 0.006, calls: 4 } }),

  // ── identidades ─────────────────────────────────────────────────────────────
  n({
    id: 2,
    parentId: 1,
    seq: 0,
    kind: "module",
    moduleSlug: "identidades",
    label: "identidades · 2 menciones",
    status: "ok",
    cost: { ownUsd: 0, subtreeUsd: 0.0033, calls: 2 },
  }),
  n({
    id: 14,
    parentId: 2,
    seq: 0,
    kind: "llm",
    label: "extracción (LLM)",
    status: "ok",
    llmCallId: 100,
    cost: { ownUsd: 0.0021, subtreeUsd: 0.0021, calls: 1 },
    llm: {
      model: DS,
      promptTokens: 1840,
      completionTokens: 96,
      latencyMs: 1120,
      status: "ok",
      responseText:
        '{"items":[{"mentioned_name":"Juan Pérez","mentioned_kind":"persona","email":"juan.perez@acme.com"},' +
        '{"mentioned_name":"J. Pérez","mentioned_kind":"persona"}]}',
    },
  }),
  // mención 1: creada nueva (no se parece a nada → no hay dedup)
  n({
    id: 3,
    parentId: 2,
    seq: 1,
    kind: "entity",
    label: "«Juan Pérez» → creada",
    status: "ok",
    ref: { table: "mod_identidades", id: 123 },
  }),
  n({ id: 4, parentId: 3, seq: 0, kind: "log", label: "no se parece a nada → creada nueva", status: "info" }),
  // mención 2: zona gris → dedup contra «Juan Pérez» #123 → desempate LLM → consolidada
  n({
    id: 5,
    parentId: 2,
    seq: 2,
    kind: "entity",
    label: "«J. Pérez» → consolidada",
    status: "ok",
    ref: { table: "mod_identidades", id: 45 },
    cost: { ownUsd: 0, subtreeUsd: 0.0012, calls: 1 },
  }),
  n({
    id: 6,
    parentId: 5,
    seq: 0,
    kind: "step",
    label: "dedup · 1 candidato",
    cost: { ownUsd: 0, subtreeUsd: 0.0012, calls: 1 },
  }),
  n({
    id: 16,
    parentId: 6,
    seq: 0,
    kind: "decision",
    label: "vs «Juan Pérez» #123",
    status: "warn",
    ref: { table: "mod_identidades", id: 123 },
    detail: { trgm: 0.82, umbral: 0.9, zona: "gris", decidió: "LLM", resultado: "misma entidad" },
    cost: { ownUsd: 0, subtreeUsd: 0.0012, calls: 1 },
  }),
  n({
    id: 7,
    parentId: 16,
    seq: 0,
    kind: "llm",
    label: "desempate LLM",
    status: "ok",
    llmCallId: 101,
    cost: { ownUsd: 0.0012, subtreeUsd: 0.0012, calls: 1 },
    llm: {
      model: DS,
      promptTokens: 640,
      completionTokens: 58,
      latencyMs: 880,
      status: "ok",
      responseText:
        '{"same_entity":true,"confidence":0.91,"rationale":"Mismo apellido y dominio de correo; ' +
        '«J. Pérez» es abreviación de «Juan Pérez»."}',
    },
  }),
  n({ id: 8, parentId: 5, seq: 1, kind: "log", label: "consolidada con «Juan Pérez» #123", status: "ok" }),

  // ── finance ─────────────────────────────────────────────────────────────────
  n({
    id: 9,
    parentId: 1,
    seq: 1,
    kind: "module",
    moduleSlug: "finance",
    label: "finance · 1 transacción",
    status: "ok",
    cost: { ownUsd: 0, subtreeUsd: 0.0027, calls: 2 },
  }),
  n({
    id: 15,
    parentId: 9,
    seq: 0,
    kind: "llm",
    label: "extracción (LLM)",
    status: "ok",
    llmCallId: 102,
    cost: { ownUsd: 0.0019, subtreeUsd: 0.0019, calls: 1 },
    llm: {
      model: DS,
      promptTokens: 1720,
      completionTokens: 74,
      latencyMs: 1040,
      status: "ok",
      responseText:
        '{"items":[{"direction":"egreso","amount":50000,"currency":"COP","counterparty":"Uber",' +
        '"category":"transporte","occurred_at":"2026-06-04"}]}',
    },
  }),
  n({
    id: 10,
    parentId: 9,
    seq: 1,
    kind: "entity",
    label: "egreso $50.000 COP · Uber",
    status: "ok",
    ref: { table: "mod_finance_transactions", id: 100 },
    cost: { ownUsd: 0, subtreeUsd: 0.0008, calls: 1 },
  }),
  // seam contraparte → identidad (determinístico, sin LLM)
  n({
    id: 11,
    parentId: 10,
    seq: 0,
    kind: "decision",
    label: "contraparte → identidad «Uber» #210",
    status: "ok",
    ref: { table: "mod_identidades", id: 210 },
    detail: { método: "email", contraparte: "Uber" },
  }),
  // dedup contra una transacción previa (no es duplicado)
  n({
    id: 12,
    parentId: 10,
    seq: 1,
    kind: "step",
    label: "dedup · 1 candidato",
    cost: { ownUsd: 0, subtreeUsd: 0.0008, calls: 1 },
  }),
  n({
    id: 17,
    parentId: 12,
    seq: 0,
    kind: "decision",
    label: "vs tx #87",
    status: "ok",
    ref: { table: "mod_finance_transactions", id: 87 },
    detail: { montos: "$50.000 vs $48.000", fechas: "04-jun vs 02-jun", decidió: "LLM", resultado: "no es duplicado" },
    cost: { ownUsd: 0, subtreeUsd: 0.0008, calls: 1 },
  }),
  n({
    id: 13,
    parentId: 17,
    seq: 0,
    kind: "llm",
    label: "desempate dedup (LLM)",
    status: "ok",
    llmCallId: 103,
    cost: { ownUsd: 0.0008, subtreeUsd: 0.0008, calls: 1 },
    llm: {
      model: DS,
      promptTokens: 520,
      completionTokens: 44,
      latencyMs: 760,
      status: "ok",
      responseText:
        '{"duplicate":false,"confidence":0.74,"rationale":"Montos y fechas distintos; no es la misma transacción."}',
    },
  }),
]
