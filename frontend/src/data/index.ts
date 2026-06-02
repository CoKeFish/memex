// Facade único de datos del dashboard. Toda la UI importa de `@/data`.
//
// HOY: la superficie de CORREOS sale de la API real (./email); el resto reexporta selectores,
// catálogo y seeds mock para que la app compile y funcione end-to-end. A medida que cada dominio
// gane endpoints, se migra acá sin tocar las vistas.

// ---- Correos (datos reales contra la API) -------------------------------------
export * from "./email"

// ---- Filtros (filter_rules, datos reales) -------------------------------------
export * from "./filters"

// ---- Redes sociales monitoreadas (sources social + allowlist, datos reales) ---
export * from "./social"

// ---- Métricas de costo LLM (datos reales contra la API) -----------------------
export * from "./metrics"
// Catálogo de módulos (etiquetas/colores) — el corte por módulo lo agrega el backend.
export { MODULES, moduleChart, moduleLabel } from "@/lib/metrics"

// ---- Observabilidad del pipeline (datos reales contra la API: router /stats) --
// fetchPipeline (salud por fuente + workers + corridas de ingesta) y fetchOverview (contadores del
// /resumen). Reemplazan a los selectores mock sourceHealth/workerLatest/ingestion*/*Count de abajo.
export * from "./pipeline"

// ---- Procesamiento (datos reales: routers /modules y /processing) -------------
// Toggle de fuentes/módulos + cobertura, control del scheduler y corridas por lote (dry-run + run en
// background + polling). Reemplaza los getters mock getSources/getModuleSettings/getScheduler*/dryRunRun.
export * from "./processing"

// ---- Selectores de agregación (mock) ------------------------------------------
// Nota: los selectores de costo LLM (costKpis/costDaily/costBy*/callsInRange) se RETIRARON del
// facade: la vista /metricas ahora consume `./metrics` (API real). Siguen viviendo en lib/selectors
// para los mocks, pero ya no se reexportan acá.
export {
  inboxErrorCount,
  inboxPendingCount,
  ingestionTotals,
  ingestionWithInvariant,
  reviewCount,
  sourceHealth,
  staleWorkerCount,
  workerLatest,
} from "@/lib/selectors"

// ---- Finance (datos reales contra la API) -------------------------------------
export * from "./finance"
// Agregaciones puras (operan sobre los gastos que trae ./finance) + catálogo de categorías.
export {
  CATEGORIES,
  CATEGORY_CHART,
  CATEGORY_LABEL,
  financeByCategory,
  financeByMerchant,
  financeByMonth,
  financeCurrencies,
  financeKpis,
} from "@/lib/finance"

// ---- Catálogo / constantes (mock) ---------------------------------------------
export { JOB_LABEL, JOBS, MODEL_PRICING, PURPOSES, PURPOSE_LABEL, SOURCE_BY_ID } from "@/mocks/catalog"
export { NOW } from "@/mocks"
export { dryRunFetch } from "@/mocks/control"
export { getMessageJourney } from "@/mocks/journey"

// ---- Logs del sistema (datos reales: /metrics/llm/calls + /stats/pipeline) ----
// Stream reconstruido de llm_calls + timeline de observabilidad del pipeline (reemplazan los
// mocks getLogEvents/buildObsTimeline).
export * from "./logs"

// ---- Getters mock síncronos sobre los seeds existentes ------------------------
import { account } from "@/mocks/account"
import {
  calendarConflicts,
  calendarSyncRuns,
  consolidatedEvents,
  dedupDecisions,
} from "@/mocks/calendar"
import { inbox, reviewItems, seedAlerts } from "@/mocks"
import type {
  Account,
  AlertEvent,
  CalendarConflict,
  CalendarSyncRun,
  ConsolidatedEvent,
  DedupDecision,
  InboxRow,
  ReviewItem,
} from "@/types/domain"

export function getInbox(): InboxRow[] {
  return inbox
}

export function getReviewItems(): ReviewItem[] {
  return reviewItems
}

export function getSeedAlerts(): AlertEvent[] {
  return seedAlerts
}

export function getAccount(): Account {
  return account
}

export function getCalendarEvents(): ConsolidatedEvent[] {
  return consolidatedEvents
}

export function getCalendarConflicts(): CalendarConflict[] {
  return calendarConflicts
}

export function getCalendarSyncRuns(): CalendarSyncRun[] {
  return calendarSyncRuns
}

export function getDedupDecisions(): DedupDecision[] {
  return dedupDecisions
}
