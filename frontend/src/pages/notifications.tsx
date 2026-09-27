import { useMemo, useState } from "react"
import { Check, Loader2, X } from "lucide-react"
import { useNavigate } from "react-router-dom"
import { cn } from "@/lib/utils"
import { PageHeader } from "@/components/common/page-header"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { Led } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import {
  dismissNotification,
  fetchNotifications,
  markNotificationRead,
  readAllNotifications,
} from "@/data"
import { useAsync } from "@/lib/use-async"
import { NotificationsSettings } from "@/components/features/notifications/notifications-settings"
import type { Tone } from "@/lib/status"
import type { AlertSeverity, PersistedNotification } from "@/types/domain"

const sevTone: Record<AlertSeverity, Tone> = { critica: "error", alta: "review", info: "running" }

export function NotificationsPage() {
  const { data, loading, error, reload } = useAsync(() => fetchNotifications(), [])
  const nav = useNavigate()
  const items = useMemo(() => data?.items ?? [], [data])
  const unread = data?.unread ?? 0
  const [kind, setKind] = useState<string>("todos")
  const [audience, setAudience] = useState<string>("todas")

  const kinds = useMemo(() => Array.from(new Set(items.map((n) => n.kind))), [items])
  const rows = useMemo(
    () =>
      items.filter(
        (n) => (kind === "todos" || n.kind === kind) && (audience === "todas" || n.audience === audience),
      ),
    [items, kind, audience],
  )

  const onOpen = (n: PersistedNotification) => {
    if (n.readAt === null) void markNotificationRead(n.id).catch(() => {})
    if (n.deepLink) nav(n.deepLink)
    else reload()
  }
  const onRead = (n: PersistedNotification) => void markNotificationRead(n.id).finally(() => reload())
  const onDismiss = (n: PersistedNotification) => void dismissNotification(n.id).finally(() => reload())
  const onReadAll = () => void readAllNotifications().finally(() => reload())

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="centro · notificaciones"
        title="Notificaciones"
        description="La cola de avisos: lo personal (resumen del día, recordatorios, plazos, pagos) y lo interno de memex (ingestas o workers con problemas). Los mismos avisos aparecen en la campana y, si hay un canal, llegan por Telegram. Abajo se configura el daemon: qué avisa, cuándo y por dónde."
        actions={
          <div className="flex items-center gap-2">
            <Select value={audience} onValueChange={setAudience}>
              <SelectTrigger className="h-8 w-auto min-w-[120px] text-xs" aria-label="Audiencia">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="todas" className="text-xs">Todas</SelectItem>
                <SelectItem value="personal" className="text-xs">Personal</SelectItem>
                <SelectItem value="sistema" className="text-xs">Sistema</SelectItem>
              </SelectContent>
            </Select>
            {kinds.length > 1 && (
              <Select value={kind} onValueChange={setKind}>
                <SelectTrigger className="h-8 w-auto min-w-[130px] text-xs" aria-label="Tipo">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="todos" className="text-xs">Todos los tipos</SelectItem>
                  {kinds.map((k) => (
                    <SelectItem key={k} value={k} className="text-xs">{k}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            {unread > 0 && (
              <Button variant="outline" size="sm" className="h-8 text-xs" onClick={onReadAll}>
                <Check className="size-3.5" /> Marcar todas
              </Button>
            )}
          </div>
        }
      />
      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading && !data ? (
        <div className="flex items-center justify-center gap-2 py-24 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Cargando notificaciones…
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          title="Sin notificaciones"
          hint="No hay avisos activos. Cuando el daemon tenga algo que avisarte (el resumen del día, un evento próximo, una tarea que vence), aparecerá acá y en la campana."
        />
      ) : (
        <ul className="divide-y divide-border overflow-hidden rounded-lg border">
          {rows.map((n) => (
            <li
              key={n.id}
              className={cn("flex items-start gap-3 px-4 py-3", n.readAt === null && "bg-brand/5")}
            >
              <Led tone={sevTone[n.severity]} className="mt-1.5" />
              <button onClick={() => onOpen(n)} className="min-w-0 flex-1 text-left">
                <div className="flex items-center justify-between gap-2">
                  <span
                    className={cn(
                      "text-sm",
                      n.readAt !== null ? "text-muted-foreground" : "font-medium text-foreground",
                    )}
                  >
                    {n.title}
                  </span>
                  <span className="shrink-0 text-[11px] text-muted-foreground">
                    <RelativeTime date={n.createdAt} />
                  </span>
                </div>
                {n.body && <p className="mt-0.5 text-xs text-muted-foreground">{n.body}</p>}
                <span className="mt-1 inline-block text-[10px] uppercase tracking-wide text-muted-foreground/70">
                  {n.audience} · {n.kind}
                </span>
              </button>
              <div className="flex shrink-0 items-center gap-1">
                {n.readAt === null && (
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-7"
                    aria-label="Marcar leído"
                    onClick={() => onRead(n)}
                  >
                    <Check className="size-3.5" />
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  className="size-7"
                  aria-label="Descartar"
                  onClick={() => onDismiss(n)}
                >
                  <X className="size-3.5" />
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
      <NotificationsSettings />
    </div>
  )
}
