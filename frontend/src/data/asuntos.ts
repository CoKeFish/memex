// Superficie del dominio ASUNTOS (storylines: hilos de vida inferidos de los mensajes) contra la
// API real (router /asuntos). Como `identidades.ts`: funciones async + transforms snake_case →
// camelCase. La vista `/asuntos` consume la lista (con contadores por estado) y `/asuntos/:id` la
// storyline completa (hitos, evidencia por medio, historial de estados, relaciones y elenco).

import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api"

// ---- Tipos del dominio (camelCase) ------------------------------------------------------------

export type AsuntoKind =
  | "evento"
  | "programa"
  | "hackathon"
  | "tramite"
  | "proyecto"
  | "comunidad"
  | "servicio"
  | "otro"

export type AsuntoStatus = "candidato" | "abierto" | "cerrado" | "inactivo"
/** Prioridad del seguimiento (Outlook/Jira): alta = plazo, plata o derecho en juego; baja = pasivo. */
export type AsuntoPriority = "alta" | "normal" | "baja"

export type AsuntoEvidenceMethod =
  | "llm"
  | "hilo"
  | "evento"
  | "asunto"
  | "manual"
  | "agent"
  | "curator"

export type AsuntoRelationType = "parte_de" | "requisito_de" | "continua"

export interface Asunto {
  id: number
  name: string
  aliases: string[]
  kind: AsuntoKind
  description: string
  status: AsuntoStatus
  stateText: string
  stateUpdatedAt: string | null
  seed: string
  entities: string[]
  openedByInboxId: number | null
  firstSignalAt: string | null
  lastSignalAt: string | null
  evidenceCount: number
  closedAt: string | null
  mergedIntoId: number | null
  createdAt: string
  updatedAt: string
  priority: AsuntoPriority
  priorityReason: string
  prioritySource: string | null
  dueOn: string | null
  /** Silenciado por el dueño: queda cerrado aunque siga llegando señal y nadie más lo reabre. */
  silencedAt: string | null
  /** «Qué sigue»: tareas pendientes vivas (los proyectos no cuentan) y la fecha más próxima. */
  tareasPendientes: number
  proximaFecha: string | null
}

export interface AsuntoList {
  items: Asunto[]
  counts: Partial<Record<AsuntoStatus, number>>
}

export interface AsuntoHito {
  /** YYYY-MM-DD o null si el hito no tiene fecha. */
  date: string | null
  dateIsExact: boolean
  text: string | null
  /** Mensaje de origen; null cuando el hito lo contó el dueño (`notaId`). */
  inboxId: number | null
  evidenceId: number | null
  notaId: number | null
}

/** Lo que contó el dueño, con lo que el sistema entendió e hizo por eso. */
export interface AsuntoNota {
  id: number
  texto: string
  entendido: string
  cambios: string[]
  createdAt: string
}

export interface AsuntoEvidence {
  id: number
  inboxId: number
  /** Medio del mensaje (email|chat|social); null si la fila ya no existe. */
  kind: string | null
  method: AsuntoEvidenceMethod
  confidence: number
  occurredAt: string | null
  subject: string
  sender: string
  hitoText: string | null
  hitoDate: string | null
  /** Documento adjunto: nombre real, id del blob (para bajarlo) y tamaño; null en mensajes. */
  filename: string | null
  mediaId: number | null
  sizeBytes: number | null
}

export interface AsuntoState {
  id: number
  stateText: string
  closed: boolean
  source: string
  inboxId: number | null
  createdAt: string
}

export interface AsuntoRelation {
  id: number
  srcAsuntoId: number
  dstAsuntoId: number
  relationType: AsuntoRelationType
  confidence: number
  source: string
  otherId: number
  otherName: string
  otherStatus: AsuntoStatus
  /** "out" = este asunto es el origen (src); "in" = es el destino (dst). */
  direction: "out" | "in"
}

export interface AsuntoElencoItem {
  slug: string
  id: number
  label: string
  kind: string
  messages: number
  edgeId: number
}

export interface AsuntoDato {
  label: string
  value: string
  inboxIds: number[]
  evidence: string
  origin: string
  at: string | null
}

export interface AsuntoStory {
  notas: AsuntoNota[]
  asunto: Asunto
  datos: AsuntoDato[]
  hitos: AsuntoHito[]
  evidence: AsuntoEvidence[]
  states: AsuntoState[]
  relations: AsuntoRelation[]
  elenco: AsuntoElencoItem[]
}

export interface AsuntoAssignStats {
  considered: number
  relevant: number
  skippedSeen: number
  adhered: number
  llmBatches: number
  assigned: number
  none: number
  created: number
  promoted: number
  hitos: number
  updates: number
  closed: number
  relations: number
  lowConfidence: number
  dropped: number
  errors: number
  woven: number
  costUsd: number
  touched: number
}

export interface AsuntoCurateInput {
  scope: "duplicates" | "candidates" | "open" | "relations" | "all"
  limit?: number
  dryRun?: boolean
  webSearch?: boolean
}

export interface AsuntoCurateStats {
  items: number
  calls: number
  merged: number
  renamed: number
  closed: number
  reopened: number
  deleted: number
  kept: number
  related: number
  retyped: number
  skipped: number
  dropped: number
  errors: number
  applied: {
    item: string
    action: string
    result: string
    confidence?: number
    rationale?: string | null
  }[]
}

export interface AsuntosSettings {
  enabled: boolean
  adhesionEnabled: boolean
  batchSize: number
  bodyChars: number
  closedWindowDays: number
  inactiveDays: number
  minEvidenceOpen: number
  minConfidence: number
}

export type AsuntosSettingsPatch = Partial<AsuntosSettings>

// ---- Shapes crudos del API (snake_case) -------------------------------------------------------

interface AsuntoApi {
  id: number
  name: string
  aliases: string[]
  kind: AsuntoKind
  description: string
  status: AsuntoStatus
  state_text: string
  state_updated_at: string | null
  seed: string
  entities: string[]
  opened_by_inbox_id: number | null
  first_signal_at: string | null
  last_signal_at: string | null
  evidence_count: number
  closed_at: string | null
  merged_into_id: number | null
  created_at: string
  updated_at: string
  priority?: AsuntoPriority
  priority_reason?: string
  priority_source?: string | null
  due_on?: string | null
  silenced_at?: string | null
  tareas_pendientes?: number
  proxima_fecha?: string | null
}

interface HitoApi {
  date: string | null
  date_is_exact: boolean
  text: string | null
  inbox_id: number | null
  evidence_id: number | null
  nota_id?: number | null
}

interface NotaApi {
  id: number
  texto: string
  entendido: string
  cambios: string[]
  created_at: string
}

interface EvidenceApi {
  id: number
  inbox_id: number
  kind: string | null
  method: AsuntoEvidenceMethod
  confidence: number
  occurred_at: string | null
  subject: string
  sender: string
  hito_text: string | null
  hito_date: string | null
  filename?: string | null
  media_id?: number | null
  size_bytes?: number | null
}

interface StateApi {
  id: number
  state_text: string
  closed: boolean
  source: string
  inbox_id: number | null
  created_at: string
}

interface RelationApi {
  id: number
  src_asunto_id: number
  dst_asunto_id: number
  relation_type: AsuntoRelationType
  confidence: number
  source: string
  other_id: number
  other_name: string
  other_status: AsuntoStatus
  direction: "out" | "in"
}

interface ElencoApi {
  slug: string
  id: number
  label: string
  kind: string
  messages: number
  edge_id: number
}

interface DatoApi {
  label?: string
  value: string
  inbox_ids?: number[]
  evidence?: string
  origin?: string
  at?: string | null
}

interface StoryApi {
  asunto: AsuntoApi
  datos?: DatoApi[]
  hitos: HitoApi[]
  evidence: EvidenceApi[]
  states: StateApi[]
  notas?: NotaApi[]
  relations: RelationApi[]
  elenco: ElencoApi[]
}

interface AssignStatsApi {
  considered?: number
  relevant?: number
  skipped_seen?: number
  adhered?: number
  llm_batches?: number
  assigned?: number
  none?: number
  created?: number
  promoted?: number
  hitos?: number
  updates?: number
  closed?: number
  relations?: number
  low_confidence?: number
  dropped?: number
  errors?: number
  woven?: number
  cost_usd?: number
  touched?: number
}

interface CurateStatsApi {
  items?: number
  calls?: number
  merged?: number
  renamed?: number
  closed?: number
  reopened?: number
  deleted?: number
  kept?: number
  related?: number
  retyped?: number
  skipped?: number
  dropped?: number
  errors?: number
  applied?: {
    item: string
    action: string
    result: string
    confidence?: number
    rationale?: string | null
  }[]
}

interface SettingsApi {
  enabled: boolean
  adhesion_enabled: boolean
  batch_size: number
  body_chars: number
  closed_window_days: number
  inactive_days: number
  min_evidence_open: number
  min_confidence: number
}

// ---- Transforms -------------------------------------------------------------------------------

function toAsunto(a: AsuntoApi): Asunto {
  return {
    id: a.id,
    name: a.name,
    aliases: a.aliases ?? [],
    kind: a.kind,
    description: a.description,
    status: a.status,
    stateText: a.state_text,
    stateUpdatedAt: a.state_updated_at,
    seed: a.seed,
    entities: a.entities ?? [],
    openedByInboxId: a.opened_by_inbox_id,
    firstSignalAt: a.first_signal_at,
    lastSignalAt: a.last_signal_at,
    evidenceCount: a.evidence_count,
    closedAt: a.closed_at,
    mergedIntoId: a.merged_into_id,
    createdAt: a.created_at,
    updatedAt: a.updated_at,
    priority: a.priority ?? "normal",
    priorityReason: a.priority_reason ?? "",
    prioritySource: a.priority_source ?? null,
    dueOn: a.due_on ?? null,
    silencedAt: a.silenced_at ?? null,
    tareasPendientes: a.tareas_pendientes ?? 0,
    proximaFecha: a.proxima_fecha ?? null,
  }
}

function toDato(d: DatoApi): AsuntoDato {
  return {
    label: d.label ?? "dato",
    value: d.value,
    inboxIds: d.inbox_ids ?? [],
    evidence: d.evidence ?? "",
    origin: d.origin ?? "extraction",
    at: d.at ?? null,
  }
}

export async function addAsuntoDato(id: number, label: string, value: string): Promise<AsuntoDato[]> {
  const r = await apiPost<{ created: boolean; datos: DatoApi[] }>(`/asuntos/${id}/datos`, { label, value })
  return r.datos.map(toDato)
}

export async function removeAsuntoDato(id: number, value: string): Promise<AsuntoDato[]> {
  const r = await apiPost<{ removed: boolean; datos: DatoApi[] }>(`/asuntos/${id}/datos/remove`, { value })
  return r.datos.map(toDato)
}

function toHito(h: HitoApi): AsuntoHito {
  return {
    date: h.date,
    dateIsExact: h.date_is_exact,
    text: h.text,
    inboxId: h.inbox_id,
    evidenceId: h.evidence_id,
    notaId: h.nota_id ?? null,
  }
}

function toNota(n: NotaApi): AsuntoNota {
  return { id: n.id, texto: n.texto, entendido: n.entendido, cambios: n.cambios, createdAt: n.created_at }
}

function toEvidence(e: EvidenceApi): AsuntoEvidence {
  return {
    id: e.id,
    inboxId: e.inbox_id,
    kind: e.kind,
    method: e.method,
    confidence: e.confidence,
    occurredAt: e.occurred_at,
    subject: e.subject,
    sender: e.sender,
    hitoText: e.hito_text,
    hitoDate: e.hito_date,
    filename: e.filename ?? null,
    mediaId: e.media_id ?? null,
    sizeBytes: e.size_bytes ?? null,
  }
}

function toState(s: StateApi): AsuntoState {
  return {
    id: s.id,
    stateText: s.state_text,
    closed: s.closed,
    source: s.source,
    inboxId: s.inbox_id,
    createdAt: s.created_at,
  }
}

function toRelation(r: RelationApi): AsuntoRelation {
  return {
    id: r.id,
    srcAsuntoId: r.src_asunto_id,
    dstAsuntoId: r.dst_asunto_id,
    relationType: r.relation_type,
    confidence: r.confidence,
    source: r.source,
    otherId: r.other_id,
    otherName: r.other_name,
    otherStatus: r.other_status,
    direction: r.direction,
  }
}

function toElenco(e: ElencoApi): AsuntoElencoItem {
  return { slug: e.slug, id: e.id, label: e.label, kind: e.kind, messages: e.messages, edgeId: e.edge_id }
}

function toAssignStats(s: AssignStatsApi): AsuntoAssignStats {
  return {
    considered: s.considered ?? 0,
    relevant: s.relevant ?? 0,
    skippedSeen: s.skipped_seen ?? 0,
    adhered: s.adhered ?? 0,
    llmBatches: s.llm_batches ?? 0,
    assigned: s.assigned ?? 0,
    none: s.none ?? 0,
    created: s.created ?? 0,
    promoted: s.promoted ?? 0,
    hitos: s.hitos ?? 0,
    updates: s.updates ?? 0,
    closed: s.closed ?? 0,
    relations: s.relations ?? 0,
    lowConfidence: s.low_confidence ?? 0,
    dropped: s.dropped ?? 0,
    errors: s.errors ?? 0,
    woven: s.woven ?? 0,
    costUsd: s.cost_usd ?? 0,
    touched: s.touched ?? 0,
  }
}

function toCurateStats(s: CurateStatsApi): AsuntoCurateStats {
  return {
    items: s.items ?? 0,
    calls: s.calls ?? 0,
    merged: s.merged ?? 0,
    renamed: s.renamed ?? 0,
    closed: s.closed ?? 0,
    reopened: s.reopened ?? 0,
    deleted: s.deleted ?? 0,
    kept: s.kept ?? 0,
    related: s.related ?? 0,
    retyped: s.retyped ?? 0,
    skipped: s.skipped ?? 0,
    dropped: s.dropped ?? 0,
    errors: s.errors ?? 0,
    applied: s.applied ?? [],
  }
}

function toSettings(s: SettingsApi): AsuntosSettings {
  return {
    enabled: s.enabled,
    adhesionEnabled: s.adhesion_enabled,
    batchSize: s.batch_size,
    bodyChars: s.body_chars,
    closedWindowDays: s.closed_window_days,
    inactiveDays: s.inactive_days,
    minEvidenceOpen: s.min_evidence_open,
    minConfidence: s.min_confidence,
  }
}

// ---- Fetchers ---------------------------------------------------------------------------------

export async function fetchAsuntos(opts?: {
  statuses?: AsuntoStatus[]
  kind?: AsuntoKind
  priorities?: AsuntoPriority[]
  q?: string
  limit?: number
  offset?: number
}): Promise<AsuntoList> {
  const params = new URLSearchParams()
  if (opts?.statuses && opts.statuses.length > 0) params.set("status", opts.statuses.join(","))
  if (opts?.kind) params.set("kind", opts.kind)
  if (opts?.priorities && opts.priorities.length > 0) params.set("priority", opts.priorities.join(","))
  if (opts?.q) params.set("q", opts.q)
  if (opts?.limit != null) params.set("limit", String(opts.limit))
  if (opts?.offset != null) params.set("offset", String(opts.offset))
  const qs = params.toString()
  const r = await apiGet<{ items: AsuntoApi[]; counts: Record<string, number> }>(
    `/asuntos${qs ? `?${qs}` : ""}`,
  )
  return { items: r.items.map(toAsunto), counts: r.counts as AsuntoList["counts"] }
}

export async function fetchAsuntoStory(id: number): Promise<AsuntoStory> {
  const s = await apiGet<StoryApi>(`/asuntos/${id}`)
  return {
    asunto: toAsunto(s.asunto),
    datos: (s.datos ?? []).map(toDato),
    hitos: s.hitos.map(toHito),
    evidence: s.evidence.map(toEvidence),
    states: s.states.map(toState),
    notas: (s.notas ?? []).map(toNota),
    relations: s.relations.map(toRelation),
    elenco: s.elenco.map(toElenco),
  }
}

export async function fetchAsuntosSettings(): Promise<AsuntosSettings> {
  return toSettings(await apiGet<SettingsApi>("/asuntos/settings"))
}

// ---- Mutations --------------------------------------------------------------------------------

/** Qué hacer con las pendientes al cerrar: dejarlas, darlas por hechas o cancelarlas. */
export type TareasAlCerrar = "dejar" | "completar" | "cancelar"

export async function closeAsunto(
  id: number,
  stateText = "",
  tareas: TareasAlCerrar = "dejar",
): Promise<Asunto> {
  return toAsunto(
    await apiPost<AsuntoApi>(`/asuntos/${id}/close`, { state_text: stateText, tareas }),
  )
}

export async function setAsuntoPriority(id: number, priority: AsuntoPriority, reason = ""): Promise<Asunto> {
  return toAsunto(await apiPost<AsuntoApi>(`/asuntos/${id}/priority`, { priority, reason }))
}

export async function reopenAsunto(id: number, stateText = ""): Promise<Asunto> {
  return toAsunto(await apiPost<AsuntoApi>(`/asuntos/${id}/reopen`, { state_text: stateText }))
}

/** Un paso del plan, con la fuente OFICIAL de donde salió cada dato. */
export interface PlanPaso {
  titulo: string
  queHacer: string
  donde: string
  llevar: string[]
  costo: string
  fecha: string | null
  fuente: string
  /** Si había opciones: cuál se eligió, por qué, y las otras en una línea. */
  porQue: string
}

export interface Plan {
  asuntoId: number
  resumen: string
  pasos: PlanPaso[]
  fuentes: string[]
  /** Lo que NO se pudo confirmar en una fuente oficial. Vale más esto que un dato inventado. */
  sinConfirmar: string[]
  proyectoId: number | null
  creadas: number
  repetidas: number
}

interface PlanApi {
  asunto_id: number
  resumen: string
  pasos: {
    titulo: string
    que_hacer: string
    donde: string
    llevar: string[]
    costo: string
    fecha: string | null
    fuente: string
    por_que?: string
  }[]
  fuentes: string[]
  sin_confirmar: string[]
  proyecto_id: number | null
  creadas: number
  repetidas: number
}

/** Declara un asunto tuyo, que no nace de un mensaje. Es el paso previo a pedirle el plan. */
export async function createAsunto(
  name: string,
  kind: AsuntoKind = "tramite",
  description = "",
): Promise<Asunto> {
  return toAsunto(await apiPost<AsuntoApi>("/asuntos", { name, kind, description }))
}

/** Arma el plan buscando en fuentes oficiales. Tarda varios minutos: es codex con búsqueda web. */
export async function planAsunto(id: number, context = ""): Promise<Plan> {
  const r = await apiPost<PlanApi>(`/asuntos/${id}/plan`, { context })
  return {
    asuntoId: r.asunto_id,
    resumen: r.resumen,
    pasos: r.pasos.map((p) => ({
      titulo: p.titulo,
      queHacer: p.que_hacer,
      donde: p.donde,
      llevar: p.llevar,
      costo: p.costo,
      fecha: p.fecha,
      fuente: p.fuente,
      porQue: p.por_que ?? "",
    })),
    fuentes: r.fuentes,
    sinConfirmar: r.sin_confirmar,
    proyectoId: r.proyecto_id,
    creadas: r.creadas,
    repetidas: r.repetidas,
  }
}

/** Silencia el asunto: queda cerrado aunque siga llegando señal y sus pendientes se cancelan. */
/** Qué pasó con lo que contó el dueño: qué entendió el sistema y qué cambió por eso. */
export interface NotaResult {
  notaId: number
  texto: string
  entendido: string
  cambios: string[]
  /** false = quedó guardada como hito pero nadie la interpretó (sin intérprete). */
  interpretada: boolean
}

export async function anotarAsunto(id: number, texto: string): Promise<NotaResult> {
  const r = await apiPost<{
    nota_id: number
    texto: string
    entendido: string
    cambios: string[]
    interpretada: boolean
  }>(`/asuntos/${id}/nota`, { texto })
  return { notaId: r.nota_id, texto: r.texto, entendido: r.entendido, cambios: r.cambios, interpretada: r.interpretada }
}

export async function silenceAsunto(id: number): Promise<Asunto> {
  return toAsunto(await apiPost<AsuntoApi>(`/asuntos/${id}/silence`, {}))
}

/** Levanta el silencio. No lo reabre: vuelve a poder revivir con una señal nueva. */
export async function unsilenceAsunto(id: number): Promise<Asunto> {
  return toAsunto(await apiPost<AsuntoApi>(`/asuntos/${id}/unsilence`, {}))
}

export async function setAsuntoState(id: number, stateText: string): Promise<Asunto> {
  return toAsunto(await apiPost<AsuntoApi>(`/asuntos/${id}/state`, { state_text: stateText }))
}

export async function renameAsunto(id: number, name: string, kind?: AsuntoKind): Promise<Asunto> {
  const body: Record<string, unknown> = { name }
  if (kind !== undefined) body.kind = kind
  return toAsunto(await apiPost<AsuntoApi>(`/asuntos/${id}/rename`, body))
}

export async function deleteAsunto(id: number): Promise<void> {
  await apiDelete<{ deleted: boolean }>(`/asuntos/${id}`)
}

/** Fusiona `absorbedId` dentro de `survivorId`; devuelve el sobreviviente actualizado. */
export async function mergeAsuntos(survivorId: number, absorbedId: number): Promise<Asunto> {
  return toAsunto(
    await apiPost<AsuntoApi>("/asuntos/merge", { survivor_id: survivorId, absorbed_id: absorbedId }),
  )
}

export async function relateAsuntos(
  srcId: number,
  dstId: number,
  relationType: AsuntoRelationType,
): Promise<boolean> {
  const r = await apiPost<{ created: boolean }>("/asuntos/relate", {
    src_id: srcId,
    dst_id: dstId,
    relation_type: relationType,
  })
  return r.created
}

export async function rejectAsuntoRelation(relationId: number): Promise<void> {
  await apiPost<{ rejected: boolean }>(`/asuntos/relations/${relationId}/reject`, {})
}

export async function rejectAsuntoEvidence(evidenceId: number): Promise<number> {
  const r = await apiPost<{ asunto_id: number }>(`/asuntos/evidence/${evidenceId}/reject`, {})
  return r.asunto_id
}

export async function addAsuntoEvidence(
  asuntoId: number,
  input: { inboxId: number; hitoText?: string; hitoDate?: string },
): Promise<boolean> {
  const r = await apiPost<{ created: boolean }>(`/asuntos/${asuntoId}/evidence`, {
    inbox_id: input.inboxId,
    hito_text: input.hitoText ?? null,
    hito_date: input.hitoDate ?? null,
  })
  return r.created
}

/** Asignador: sin `inboxIds` procesa los mensajes relevantes pendientes (incremental). */
export async function assignAsuntos(opts?: {
  inboxIds?: number[]
  force?: boolean
  limit?: number
}): Promise<AsuntoAssignStats> {
  const body: Record<string, unknown> = {}
  if (opts?.inboxIds) body.inbox_ids = opts.inboxIds
  if (opts?.force !== undefined) body.force = opts.force
  if (opts?.limit !== undefined) body.limit = opts.limit
  return toAssignStats(await apiPost<AssignStatsApi>("/asuntos/assign", body))
}

export async function curateAsuntos(input: AsuntoCurateInput): Promise<AsuntoCurateStats> {
  const body: Record<string, unknown> = { scope: input.scope }
  if (input.limit !== undefined) body.limit = input.limit
  if (input.dryRun !== undefined) body.dry_run = input.dryRun
  if (input.webSearch !== undefined) body.web_search = input.webSearch
  return toCurateStats(await apiPost<CurateStatsApi>("/asuntos/curate", body))
}

export async function maintainAsuntos(): Promise<AsuntoCurateStats> {
  return toCurateStats(await apiPost<CurateStatsApi>("/asuntos/maintain", {}))
}

export async function patchAsuntosSettings(patch: AsuntosSettingsPatch): Promise<AsuntosSettings> {
  const body: Record<string, unknown> = {}
  if (patch.enabled !== undefined) body.enabled = patch.enabled
  if (patch.adhesionEnabled !== undefined) body.adhesion_enabled = patch.adhesionEnabled
  if (patch.batchSize !== undefined) body.batch_size = patch.batchSize
  if (patch.bodyChars !== undefined) body.body_chars = patch.bodyChars
  if (patch.closedWindowDays !== undefined) body.closed_window_days = patch.closedWindowDays
  if (patch.inactiveDays !== undefined) body.inactive_days = patch.inactiveDays
  if (patch.minEvidenceOpen !== undefined) body.min_evidence_open = patch.minEvidenceOpen
  if (patch.minConfidence !== undefined) body.min_confidence = patch.minConfidence
  return toSettings(await apiPatch<SettingsApi>("/asuntos/settings", body))
}
