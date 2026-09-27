// Camino de procesamiento de UN mensaje: qué etapas ya pasó, cuál sigue, cuáles no aplican y
// cuál falló — derivado SOLO de lo que el backend persiste para ese mensaje (clasificación,
// veredicto del gate, llamadas LLM atribuidas, extracción, adjuntos, fallas). Puro y testeable;
// la tarjeta (`pipeline-rail.tsx`) solo dibuja.
//
// El orden es el real del orquestador (`modules/orchestrator.py::_process_window` + lo previo):
//   ingesta → adjuntos/OCR → clasificación (tier) → gate de relevancia → ruteo → extracción →
//   resolución de identidades → grafo (relaciones).
// Las etapas cross-mensaje (atribución de dominio, co-ocurrencia, cúmulos) no dejan rastro por
// mensaje hoy: el grafo se muestra como «sin traza por mensaje», no como hecho.

import type { InboxRow, RelevanceVerdict } from "@/types/domain"

export type StepKey =
  | "ingest"
  | "media"
  | "classify"
  | "relevance"
  | "route"
  | "extract"
  | "resolve"
  | "graph"

/**
 * - done: la etapa corrió y hay evidencia persistida.
 * - next: la PRIMERA pendiente en orden — lo que toca hacer ahora.
 * - pending: falta, pero antes hay otra que hacer.
 * - blocked: espera una decisión humana (relevancia en duda).
 * - skipped: no aplica para este mensaje (p. ej. no relevante → no se extrae).
 * - failed: se intentó y falló (work_item_failures).
 * - unknown: el sistema no registra esta etapa por mensaje (hueco de traza, no un pendiente).
 */
export type StepState = "done" | "next" | "pending" | "blocked" | "skipped" | "failed" | "unknown"

export interface PipelineStep {
  key: StepKey
  label: string
  state: StepState
  /** El hecho decisivo de la etapa, corto y en mono: «regla list_unsubscribe», «juez LLM · relevante». */
  fact: string | null
  /** Cuándo (ISO) — si se conoce. */
  at: string | null
  /** Explicación para el tooltip / nota (qué hacer, por qué no aplica, el error). */
  detail: string | null
  /** Costo LLM atribuido a la etapa (USD) — null si no gastó o no se sabe. */
  costUsd: number | null
  /** id de la sección de la página a la que salta el chip «siguiente» (null = sin destino). */
  anchor: string | null
}

const TIER_LABEL: Record<string, string> = {
  blacklist: "blacklist",
  batch: "lote",
  individual: "individual",
}
const VERDICT_LABEL: Record<string, string> = {
  relevant: "relevante",
  not_relevant: "no relevante",
  insufficient: "en duda",
}
const METHOD_LABEL: Record<string, string> = { rule: "regla", llm: "juez LLM", manual: "manual" }

function declaredAttachmentCount(payload: unknown): number {
  const p = payload as { attachments?: unknown }
  return Array.isArray(p?.attachments) ? p.attachments.length : 0
}

function sumCost(calls: { costUsd: number }[]): number | null {
  if (calls.length === 0) return null
  return calls.reduce((a, c) => a + (Number.isFinite(c.costUsd) ? c.costUsd : 0), 0)
}

function verdictFact(v: RelevanceVerdict): string {
  return `${METHOD_LABEL[v.method] ?? v.method} · ${VERDICT_LABEL[v.verdict] ?? v.verdict}`
}

export function buildPipelineSteps(row: InboxRow): PipelineStep[] {
  const calls = row.llm?.items ?? []
  const failures = row.failures ?? []
  const failFor = (stage: string) => failures.find((f) => f.stage === stage) ?? null
  const steps: PipelineStep[] = []

  // 1 · Ingesta — si el mensaje existe, pasó.
  steps.push({
    key: "ingest",
    label: "Ingesta",
    state: "done",
    fact: "en memex",
    at: row.receivedAt,
    detail: "El mensaje llegó y quedó guardado.",
    costUsd: null,
    anchor: null,
  })

  // 2 · Adjuntos / OCR — solo si el mensaje los tiene. Va ANTES de la extracción porque el texto
  // OCR forma parte de lo que lee el LLM.
  const media = row.media ?? []
  const declared = declaredAttachmentCount(row.payload)
  if (media.length > 0 || declared > 0) {
    const ok = media.filter((m) => m.ocrStatus === "ok" || m.ocrStatus === "skipped").length
    const err = media.filter((m) => m.ocrStatus === "error").length
    const pend = media.filter((m) => m.ocrStatus === "pending").length
    const unstored = Math.max(0, declared - media.length)
    let state: StepState = "done"
    let fact = `${ok}/${media.length} OCR`
    let detail = "Adjuntos almacenados y transcriptos."
    if (err > 0) {
      state = "failed"
      fact = `${err} con error`
      detail = "Al menos un adjunto no se pudo transcribir; re-OCR desde «Reprocesar»."
    } else if (pend > 0 || unstored > 0) {
      state = "pending"
      fact = unstored > 0 ? `${unstored} sin bajar` : `${pend} sin OCR`
      detail =
        unstored > 0
          ? "Hay adjuntos declarados que no se almacenaron (extract_media apagado al ingerir)."
          : "Adjuntos guardados a la espera del worker de OCR."
    }
    steps.push({
      key: "media",
      label: "Adjuntos",
      state,
      fact,
      at: null,
      detail,
      costUsd: sumCost(calls.filter((c) => c.purpose === "ocr")),
      anchor: "adjuntos",
    })
  }

  // 3 · Clasificación (tier determinista).
  const cls = row.classification ?? null
  const clsRule = typeof cls?.metadata?.rule === "string" ? cls.metadata.rule : null
  steps.push({
    key: "classify",
    label: "Clasificación",
    state: cls ? "done" : "pending",
    fact: cls ? `${TIER_LABEL[cls.tier] ?? cls.tier}${clsRule ? ` · ${clsRule}` : ""}` : null,
    at: null,
    detail: cls
      ? "Tier determinista por cabeceras: es un dial de costo, no la decisión final."
      : "Sin tier todavía: clasificar es determinista y no gasta LLM.",
    costUsd: null,
    anchor: "fase-clasificacion",
  })

  // 4 · Gate de relevancia — la decisión de si se procesa. La marca manual manda. Si el mensaje
  // ya se extrajo SIN veredicto (extracción a mano desde el detalle, o gate apagado), el gate no
  // quedó pendiente: quedó OMITIDO, y no puede ser «siguiente» de algo que ya pasó.
  const mark = row.relevance ?? null
  const verdict = row.relevanceVerdict ?? null
  const relFail = failFor("relevance")
  const alreadyExtracted = !!row.extraction?.done
  let relState: StepState = "pending"
  let relFact: string | null = null
  let relDetail = "Se decide en una corrida de procesamiento (/procesamiento), o marcala a mano."
  if (mark) {
    relState = "done"
    relFact = `manual · ${mark.isRelevant ? "relevante" : "no relevante"}`
    relDetail = "Marca manual del dueño: el gate no la pisa."
  } else if (verdict) {
    relState = "done"
    relFact = verdictFact(verdict)
    relDetail = verdict.reason ? `«${verdict.reason}»` : "Veredicto del gate de relevancia."
  } else if (relFail) {
    relState = "failed"
    relFact = `falló ${relFail.attempts}×`
    relDetail = relFail.lastError
  } else if (alreadyExtracted) {
    relState = "skipped"
    relFact = "sin veredicto"
    relDetail =
      "Se procesó sin pasar por el gate (extracción a mano o gate apagado). Si querés que el sistema aprenda de este correo, marcá la relevancia."
  }
  steps.push({
    key: "relevance",
    label: "Relevancia",
    state: relState,
    fact: relFact,
    at: mark?.updatedAt ?? verdict?.createdAt ?? relFail?.updatedAt ?? null,
    detail: relDetail,
    costUsd: sumCost(calls.filter((c) => c.purpose === "relevance_gate")),
    anchor: null,
  })

  // ¿El gate cortó el camino? (no relevante → lo demás no aplica; en duda → espera decisión).
  const notRelevant = mark ? !mark.isRelevant : verdict?.verdict === "not_relevant"
  const inDoubt = !mark && verdict?.verdict === "insufficient"
  const ext = row.extraction ?? null
  const extDone = !!ext?.done
  const downstream = (pendingState: StepState): StepState =>
    notRelevant ? "skipped" : inDoubt ? "blocked" : pendingState
  const skipDetail = notRelevant
    ? "No aplica: el gate decidió no procesarlo."
    : inDoubt
      ? "Espera tu decisión en la cola de relevancia (/filtros) o una marca manual."
      : null

  // 5 · Ruteo — qué módulos leen el mensaje (LLM), o «todos» si el ruteo está apagado.
  const routeCall = calls.find((c) => c.purpose === "module_route") ?? null
  const chosen = Array.isArray(routeCall?.metadata?.chosen)
    ? (routeCall!.metadata!.chosen as unknown[]).map(String)
    : null
  steps.push({
    key: "route",
    label: "Ruteo",
    state: routeCall || extDone ? "done" : downstream("pending"),
    fact: routeCall
      ? chosen && chosen.length > 0
        ? chosen.join(", ")
        : "ningún módulo"
      : extDone
        ? "sin ruteo · todos"
        : null,
    at: routeCall?.createdAt ?? null,
    detail:
      routeCall || extDone
        ? "Un LLM eligió qué módulos procesan el mensaje (o el ruteo estaba apagado y fueron todos)."
        : (skipDetail ?? "Corre junto con la extracción."),
    costUsd: sumCost(calls.filter((c) => c.purpose === "module_route")),
    anchor: "fase-extraccion",
  })

  // 6 · Extracción — lo que sacaron los módulos (con evidencia).
  const extFail = failFor("extract")
  const nItems = ext
    ? ext.finance.length + ext.calendar.length + ext.hackathones.length + ext.identidades.length
    : 0
  const extractCalls = calls.filter((c) => c.purpose.startsWith("extract"))
  let extState: StepState
  let extFact: string | null = null
  let extDetail: string | null
  if (extDone) {
    extState = "done"
    extFact = nItems > 0 ? `${nItems} dato${nItems === 1 ? "" : "s"}` : "sin datos"
    extDetail =
      nItems > 0
        ? `Extraído por ${ext!.modules.join(", ") || "los módulos"}; cada dato trae su evidencia.`
        : "Procesado: los módulos no encontraron nada que guardar."
  } else if (extFail) {
    extState = "failed"
    extFact = `falló ${extFail.attempts}×`
    extDetail = extFail.lastError
  } else {
    extState = downstream("pending")
    extDetail = skipDetail ?? "Gasta LLM: una llamada agrupada por ventana."
  }
  steps.push({
    key: "extract",
    label: "Extracción",
    state: extState,
    fact: extFact,
    at: extractCalls.at(-1)?.createdAt ?? extFail?.updatedAt ?? null,
    detail: extDetail,
    costUsd: sumCost(extractCalls),
    anchor: "fase-extraccion",
  })

  // 7 · Identidades — el resolver contextual (LLM) fusiona / afilia / dispone el remitente.
  const resolveCalls = calls.filter((c) => c.purpose === "identidades_resolve")
  const lastResolve = resolveCalls.at(-1) ?? null
  const nIds = ext?.identidades.length ?? 0
  let resState: StepState
  let resFact: string | null = null
  let resDetail: string
  if (lastResolve) {
    const md = lastResolve.metadata ?? {}
    const merged = Number(md.merged ?? 0)
    const affiliated = Number(md.affiliated ?? 0)
    const linked = Number(md.linked ?? 0)
    resState = "done"
    const links = affiliated + linked
    resFact = `${merged} ${merged === 1 ? "fusión" : "fusiones"} · ${links} ${links === 1 ? "vínculo" : "vínculos"}`
    resDetail = "El resolver leyó el correo completo y decidió fusiones, pertenencias y el remitente."
  } else if (extDone && nIds === 0) {
    resState = "skipped"
    resDetail = "No aplica: la extracción no dejó identidades que resolver."
  } else if (extDone) {
    resState = "pending"
    resDetail = "Hay identidades sin resolver: corre al final de la ventana si el resolver está encendido."
  } else {
    resState = downstream("pending")
    resDetail = skipDetail ?? "Corre después de la extracción, si el resolver está encendido."
  }
  steps.push({
    key: "resolve",
    label: "Identidades",
    state: resState,
    fact: resFact,
    at: lastResolve?.createdAt ?? null,
    detail: resDetail,
    costUsd: sumCost(resolveCalls),
    anchor: "traza",
  })

  // 8 · Grafo — las aristas se tejen al persistir, pero hoy no guardan de qué mensaje salieron:
  // no se puede afirmar «hecho» por mensaje. Se dice tal cual.
  steps.push({
    key: "graph",
    label: "Grafo",
    state: notRelevant ? "skipped" : "unknown",
    fact: notRelevant ? null : "sin traza por mensaje",
    at: null,
    detail: notRelevant
      ? "No aplica: el gate decidió no procesarlo."
      : "Las relaciones se tejen al guardar los datos, pero el grafo no registra el mensaje de origen; «Ver en grafo» muestra lo que hay.",
    costUsd: null,
    anchor: null,
  })

  // «Siguiente» = la primera pendiente en orden (una sola). Dos matices:
  // - si algo FALLÓ, no hay «siguiente»: primero se arregla eso (el riel lo marca en rojo);
  // - el ruteo no se dispara solo (corre dentro de la extracción), así que nunca es «siguiente»:
  //   queda pendiente y la acción es la extracción.
  const anyFailed = steps.some((s) => s.state === "failed")
  const first = anyFailed ? null : steps.find((s) => s.state === "pending" && s.key !== "route")
  if (first) first.state = "next"
  return steps
}

/** La etapa que toca ahora (o null si no hay pendientes). */
export function nextStep(steps: PipelineStep[]): PipelineStep | null {
  return steps.find((s) => s.state === "next") ?? null
}
