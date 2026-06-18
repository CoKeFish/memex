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
import type { Tone } from "@/lib/status"
import type { AlertSeverity, PersistedNotification } from "@/types/domain"

const sevTone: Record<AlertSeverity, Tone> = { critica: "error", alta: "review", info: "running" }

export function NotificationsPage() {
  const { data, loading, error, reload } = useAsync(() => fetchNotifications(), [])
  const nav = useNavigate()
  const items = useMemo(() => data?.items ?? [], [data])
  const unread = data?.unread ?? 0
  const [kind, setKind] = useState<string>("todos")

  const kinds = useMemo(() => Array.from(new Set(items.map((n) => n.kind))), [items])
  const rows = useMemo(
    () => (kind === "todos" ? items : items.filter((n) => n.kind === kind)),
    [items, kind],
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
        description="La cola de avisos del sistema (hoy: transporte — «hora de salir hacia un evento»). Los mismos avisos aparecen en la campana de la barra superior. Abrí uno para ir a su contexto; descartá los que ya no necesités."
        actions={
          <div className="flex items-center gap-2">
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
          hint="No hay avisos activos. Cuando el sistema tenga algo que avisarte (p.ej. que es hora de salir hacia un evento), aparecerá acá y en la campana."
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
                  {n.kind}
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
    </div>
  )
}
