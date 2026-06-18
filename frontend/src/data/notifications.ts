// Cola de notificaciones (datos reales: router /notifications).
//
// fetchNotifications (cola activa + conteo de no-leídas) + mark/dismiss/read-all para la página
// /notificaciones y el AlertBell. `toAlertEvent` proyecta un aviso persistido al shape de la campana
// (la campana unifica alertas dinámicas + avisos persistidos). El deep-link lo decide el emisor
// (campo del contrato), no un mapa por kind acá.

import { apiGet, apiPost } from "@/lib/api"
import type { AlertEvent, PersistedNotification } from "@/types/domain"

interface NotificationApi {
  id: number
  kind: string
  severity: PersistedNotification["severity"]
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
