// Facade único de datos del dashboard. Toda la UI importa de `@/data`.
//
// HOY: la superficie de CORREOS sale de la API real (./email); el resto reexporta selectores,
// catálogo y seeds mock para que la app compile y funcione end-to-end. A medida que cada dominio
// gane endpoints, se migra acá sin tocar las vistas.

// ---- Correos (datos reales contra la API) -------------------------------------
export * from "./email"

// ---- Feedback / calibración (datos reales: router /feedback) -------------------
// fetchFeedback (lista por estado) + setFeedbackStatus (revisado/descartado/reabrir) para /calidad.
export * from "./feedback"

// ---- Calidad: relevancia por remitente (datos reales: router /quality) ---------
// fetchSenderRelevance: remitentes rankeados por relevancia (ruido primero) para /relevancia.
export * from "./quality-senders"

// ---- Media / OCR (datos reales: router /media) --------------------------------
// fetchMediaList (lista de adjuntos con estado OCR + contexto del mensaje) para /ocr.
export * from "./media"

// ---- Backfill segmentado (importación masiva por ventanas, datos reales) ------
export * from "./backfill"

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

// ---- Ingesta agendada (datos reales: router /ingest, 0025) --------------------
// Control del daemon de ingesta server-side (cada cuánto se trae cada fuente) + historial de
// corridas con su origen (manual/daemon/backfill/agent) para linkear a /logs?run_id=.
export * from "./ingest-scheduler"

// ---- Cobertura temporal (datos reales: GET /inbox/coverage) --------------------
// fetchInboxCoverage: rangos de fechas de origen ya ingeridos, por fuente (timeline de /carga).
// `toCoverage` es el transform genérico del shape lanes/ranges, reusable por endpoints futuros.
export * from "./coverage"

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

// ---- Calendar (dominio bidireccional, datos reales contra la API: router /calendar) -----------
// fetchCalendarEvents (capa consolidada + miembros), fetchDedupDecisions, fetchCalendarConflicts,
// fetchCalendarSyncRuns, fetchCalendarProviderAccounts. Reemplazan los getters mock de calendario.
export * from "./calendar"

// ---- Hackathones (datos reales contra la API) ---------------------------------
export * from "./hackathones"

// ---- Identidades (módulo, datos reales contra la API: router /identidades) ---------------------
// fetchIdentityPersons/Orgs/Mentions/ProviderAccounts/SyncRuns + mutaciones (CRUD de orgs, sync).
export * from "./identidades"

// ---- Grafo de relaciones (vértices + aristas, datos reales: router /graph) ----
// fetchGraph (vértices proyectados + aristas con productor y nivel pista/confirmed) + buildGraph
// (corre el paso determinista on-demand).
export * from "./graph"

// ---- Bienestar (datos reales contra la API: router /bienestar) ----------------
// fetchBienestarRegistros/Summary/Habits (lectura) + create/deleteBienestarHabit (alta/baja de
// hábitos desde el dashboard; los registros siguen entrando por la CLI/agente).
export * from "./bienestar"

// ---- Finance (datos reales contra la API) -------------------------------------
export * from "./finance"
// Agregaciones puras (operan sobre las transacciones que trae ./finance) + catálogo de categorías.
export {
  CATEGORIES,
  CATEGORY_CHART,
  CATEGORY_LABEL,
  financeByCategory,
  financeByMerchant,
  financeByMonth,
  financeCurrencies,
  financeKpis,
  financeMonthSummary,
} from "@/lib/finance"

// ---- Catálogo / constantes (mock) ---------------------------------------------
export { JOB_LABEL, JOBS, MODEL_PRICING, PURPOSES, PURPOSE_LABEL, SOURCE_BY_ID } from "@/mocks/catalog"
export { NOW } from "@/mocks"
export { dryRunFetch } from "@/mocks/control"
export { getMessageJourney } from "@/mocks/journey"

// ---- Logs del sistema (datos reales: /logs + /logs/stats + /stats/pipeline) ---
// fetchLogs/fetchLogStats: stream y agregados de la tabla log_events (sink real de structlog, 0020).
// fetchObsTimeline: timeline de observabilidad del pipeline. Reemplazan el viejo stream reconstruido
// de llm_calls (fetchLogEvents) y los mocks getLogEvents/buildObsTimeline.
export * from "./logs"

// ---- Getter mock síncrono sobre el seed de cuenta (sin endpoint todavía) ------
import { account } from "@/mocks/account"
import type { Account } from "@/types/domain"

export function getAccount(): Account {
  return account
}
