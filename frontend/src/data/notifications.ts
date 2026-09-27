// Cola de notificaciones (datos reales: router /notifications) + configuración del daemon.
//
// fetchNotifications (cola activa + conteo de no-leídas) + mark/dismiss/read-all para la página
// /notificaciones y el AlertBell. `toAlertEvent` proyecta un aviso persistido al shape de la campana
// (la campana unifica alertas dinámicas + avisos persistidos). El deep-link lo decide el emisor
// (campo del contrato), no un mapa por kind acá.
//
// Desde 0097: ajustes del daemon (prendido, zona), productores (activo, redacción, tiempos),
// canales de entrega (telegram, log), entregas y un ciclo a demanda (dry-run por default).

import { apiDelete, apiGet, apiPatch, apiPost } from "@/lib/api"
import type { AlertEvent, NotificationAudience, PersistedNotification } from "@/types/domain"

interface NotificationApi {
  id: number
  kind: string
  severity: PersistedNotification["severity"]
  audience: NotificationAudience
  title: string
  body: string
  payload: Record<string, unknown>
  deep_link: string | null
  created_at: string
  read_at: string | null
  dismissed_at: string | null
  expires_at: string | null
}

interface NotificationListApi {
  items: NotificationApi[]
  unread: number
  next_cursor: number | null
}

function toNotification(n: NotificationApi): PersistedNotification {
  return {
    id: n.id,
    kind: n.kind,
    severity: n.severity,
    audience: n.audience ?? "personal",
    title: n.title,
    body: n.body,
    payload: n.payload,
    deepLink: n.deep_link,
    createdAt: n.created_at,
    readAt: n.read_at,
    dismissedAt: n.dismissed_at,
    expiresAt: n.expires_at,
  }
}

export interface NotificationFeed {
  items: PersistedNotification[]
  unread: number
  nextCursor: number | null
}

/** Cola activa de avisos del usuario (newest-first) + conteo de no-leídas para el badge. */
export async function fetchNotifications(): Promise<NotificationFeed> {
  const page = await apiGet<NotificationListApi>("/notifications?limit=100")
  return {
    items: page.items.map(toNotification),
    unread: page.unread,
    nextCursor: page.next_cursor,
  }
}

/** Marca un aviso como leído (sale del conteo de no-leídas). */
export async function markNotificationRead(id: number): Promise<void> {
  await apiPost(`/notifications/${id}/read`)
}

/** Descarta un aviso (lo saca de la cola activa). */
export async function dismissNotification(id: number): Promise<void> {
  await apiPost(`/notifications/${id}/dismiss`)
}

/** Marca leídos todos los avisos activos sin leer. */
export async function readAllNotifications(): Promise<void> {
  await apiPost("/notifications/read-all")
}

/** Proyecta un aviso persistido a un item del AlertBell. El id se prefija con `notif:` para no
 *  colisionar con los ids de las alertas dinámicas y para que el provider rutee read/dismiss. */
export function toAlertEvent(n: PersistedNotification): AlertEvent {
  return {
    id: `notif:${n.id}`,
    severity: n.severity,
    kind: n.kind,
    title: n.title,
    detail: n.body,
    at: n.createdAt,
    read: n.readAt !== null,
    deepLink: n.deepLink ?? "/notificaciones",
    source: "persisted",
    notifId: n.id,
  }
}

// ---- Configuración del daemon (0097) ------------------------------------------------------

export type NotificationRender = "plantilla" | "llm"
export type NotificationConfigValue = string | number | boolean | string[]

export interface NotificationProducer {
  name: string
  audience: NotificationAudience
  description: string
  enabled: boolean
  render: NotificationRender
  llmAllowed: boolean
  config: Record<string, NotificationConfigValue>
  defaultConfig: Record<string, NotificationConfigValue>
  fromDb: boolean
}

export interface NotificationChannel {
  id: number
  channel: string
  target: string
  enabled: boolean
  subscriptions: Partial<Record<NotificationAudience, PersistedNotification["severity"]>>
  quietFrom: string | null
  quietUntil: string | null
  createdAt: string
}

export interface NotificationSettings {
  daemonEnabled: boolean
  timezone: string
  producers: NotificationProducer[]
  channels: NotificationChannel[]
  channelNames: string[]
}

export interface NotificationDelivery {
  id: number
  notificationId: number
  channelId: number
  channel: string
  target: string
  status: "enviada" | "fallida"
  attempts: number
  externalRef: string | null
  error: string | null
  sentAt: string | null
  updatedAt: string
  kind: string
  audience: NotificationAudience
  severity: PersistedNotification["severity"]
  title: string
}

export interface NotificationRunResult {
  producers: string[]
  drafts: number
  enqueued: number
  skippedExisting: number
  renderedLlm: number
  pending: number
  delivered: number
  failed: number
  heldQuiet: number
  belowSeverity: number
  errors: string[]
  preview: Record<string, unknown>[]
}

interface ProducerApi {
  name: string
  audience: NotificationAudience
  description: string
  enabled: boolean
  render: NotificationRender
  llm_allowed: boolean
  config: Record<string, NotificationConfigValue>
  default_config: Record<string, NotificationConfigValue>
  from_db: boolean
}

interface ChannelApi {
  id: number
  channel: string
  target: string
  enabled: boolean
  subscriptions: NotificationChannel["subscriptions"]
  quiet_from: string | null
  quiet_until: string | null
  created_at: string
}

interface SettingsApi {
  daemon_enabled: boolean
  timezone: string
  producers: ProducerApi[]
  channels: ChannelApi[]
  channel_names: string[]
}

interface DeliveryApi {
  id: number
  notification_id: number
  channel_id: number
  channel: string
  target: string
  status: "enviada" | "fallida"
  attempts: number
  external_ref: string | null
  error: string | null
  sent_at: string | null
  updated_at: string
  kind: string
  audience: NotificationAudience
  severity: PersistedNotification["severity"]
  title: string
}

interface RunApi {
  producers: string[]
  drafts: number
  enqueued: number
  skipped_existing: number
  rendered_llm: number
  pending: number
  delivered: number
  failed: number
  held_quiet: number
  below_severity: number
  errors: string[]
  preview: Record<string, unknown>[]
}

function toProducer(p: ProducerApi): NotificationProducer {
  return {
    name: p.name,
    audience: p.audience,
    description: p.description,
    enabled: p.enabled,
    render: p.render,
    llmAllowed: p.llm_allowed,
    config: p.config,
    defaultConfig: p.default_config,
    fromDb: p.from_db,
  }
}

function toChannel(c: ChannelApi): NotificationChannel {
  return {
    id: c.id,
    channel: c.channel,
    target: c.target,
    enabled: c.enabled,
    subscriptions: c.subscriptions,
    quietFrom: c.quiet_from,
    quietUntil: c.quiet_until,
    createdAt: c.created_at,
  }
}

function toSettings(s: SettingsApi): NotificationSettings {
  return {
    daemonEnabled: s.daemon_enabled,
    timezone: s.timezone,
    producers: s.producers.map(toProducer),
    channels: s.channels.map(toChannel),
    channelNames: s.channel_names,
  }
}

function toDelivery(d: DeliveryApi): NotificationDelivery {
  return {
    id: d.id,
    notificationId: d.notification_id,
    channelId: d.channel_id,
    channel: d.channel,
    target: d.target,
    status: d.status,
    attempts: d.attempts,
    externalRef: d.external_ref,
    error: d.error,
    sentAt: d.sent_at,
    updatedAt: d.updated_at,
    kind: d.kind,
    audience: d.audience,
    severity: d.severity,
    title: d.title,
  }
}

export async function fetchNotificationSettings(): Promise<NotificationSettings> {
  return toSettings(await apiGet<SettingsApi>("/notifications/settings"))
}

export async function patchNotificationSettings(patch: {
  daemonEnabled?: boolean
  timezone?: string
}): Promise<NotificationSettings> {
  const body: Record<string, unknown> = {}
  if (patch.daemonEnabled !== undefined) body.daemon_enabled = patch.daemonEnabled
  if (patch.timezone !== undefined) body.timezone = patch.timezone
  return toSettings(await apiPatch<SettingsApi>("/notifications/settings", body))
}

export async function patchNotificationProducer(
  name: string,
  patch: {
    enabled?: boolean
    render?: NotificationRender
    config?: Record<string, NotificationConfigValue>
  },
): Promise<NotificationProducer> {
  return toProducer(await apiPatch<ProducerApi>(`/notifications/producers/${name}`, patch))
}

export async function addNotificationChannel(input: {
  channel: string
  target: string
  subscriptions: NotificationChannel["subscriptions"]
  quietFrom?: string | null
  quietUntil?: string | null
}): Promise<NotificationChannel> {
  return toChannel(
    await apiPost<ChannelApi>("/notifications/channels", {
      channel: input.channel,
      target: input.target,
      subscriptions: input.subscriptions,
      quiet_from: input.quietFrom ?? null,
      quiet_until: input.quietUntil ?? null,
    }),
  )
}

export async function patchNotificationChannel(
  id: number,
  patch: {
    enabled?: boolean
    subscriptions?: NotificationChannel["subscriptions"]
    quietFrom?: string | null
    quietUntil?: string | null
  },
): Promise<NotificationChannel> {
  const body: Record<string, unknown> = {}
  if (patch.enabled !== undefined) body.enabled = patch.enabled
  if (patch.subscriptions !== undefined) body.subscriptions = patch.subscriptions
  if (patch.quietFrom !== undefined) body.quiet_from = patch.quietFrom
  if (patch.quietUntil !== undefined) body.quiet_until = patch.quietUntil
  return toChannel(await apiPatch<ChannelApi>(`/notifications/channels/${id}`, body))
}

export async function deleteNotificationChannel(id: number): Promise<void> {
  await apiDelete(`/notifications/channels/${id}`)
}

export async function testNotificationChannel(
  id: number,
  text?: string,
): Promise<{ ok: boolean; ref: string | null }> {
  return apiPost(`/notifications/channels/${id}/test`, text ? { text } : {})
}

export async function fetchNotificationDeliveries(limit = 30): Promise<NotificationDelivery[]> {
  const rows = await apiGet<DeliveryApi[]>(`/notifications/deliveries?limit=${limit}`)
  return rows.map(toDelivery)
}

export async function runNotificationsCycle(input: {
  dryRun: boolean
  producer?: string
}): Promise<NotificationRunResult> {
  const r = await apiPost<RunApi>("/notifications/run", {
    dry_run: input.dryRun,
    producer: input.producer ?? null,
  })
  return {
    producers: r.producers,
    drafts: r.drafts,
    enqueued: r.enqueued,
    skippedExisting: r.skipped_existing,
    renderedLlm: r.rendered_llm,
    pending: r.pending,
    delivered: r.delivered,
    failed: r.failed,
    heldQuiet: r.held_quiet,
    belowSeverity: r.below_severity,
    errors: r.errors,
    preview: r.preview,
  }
}
