import type { ReactNode } from "react"
import { AlertTriangle, Inbox, RotateCw } from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { useDemoState } from "@/state/demo-state"

export function EmptyState({
  icon,
  title,
  hint,
  action,
  className,
}: {
  icon?: ReactNode
  title: string
  hint?: string
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-3 px-6 py-14 text-center", className)}>
      <div className="flex size-10 items-center justify-center rounded-lg border border-border bg-muted/40 text-muted-foreground">
        {icon ?? <Inbox className="size-5" />}
      </div>
      <div className="space-y-1">
        <p className="text-sm font-medium text-foreground">{title}</p>
        {hint && <p className="mx-auto max-w-sm text-xs text-muted-foreground">{hint}</p>}
      </div>
      {action}
    </div>
  )
}

export function ErrorState({
  title = "No se pudieron cargar los datos",
  detail,
  onRetry,
  className,
}: {
  title?: string
  detail?: string
  onRetry?: () => void
  className?: string
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center gap-3 px-6 py-14 text-center", className)}>
      <div className="flex size-10 items-center justify-center rounded-lg border border-status-error/30 bg-status-error/10 text-status-error">
        <AlertTriangle className="size-5" />
      </div>
      <div className="space-y-1">
        <p className="text-sm font-medium text-foreground">{title}</p>
        {detail && <p className="mx-auto max-w-sm font-mono text-xs text-muted-foreground">{detail}</p>}
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <RotateCw className="size-3.5" /> Reintentar
        </Button>
      )}
    </div>
  )
}

export function TableSkeleton({ rows = 8, cols = 5 }: { rows?: number; cols?: number }) {
  return (
    <div className="divide-y divide-border">
      {Array.from({ length: rows }).map((_, r) => (
        <div key={r} className="flex items-center gap-4 px-4 py-3">
          {Array.from({ length: cols }).map((_, c) => (
            <Skeleton key={c} className={cn("h-3.5", c === 0 ? "w-1/4" : "w-[12%]")} />
          ))}
        </div>
      ))}
    </div>
  )
}

export function CardsSkeleton({ count = 4 }: { count?: number }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className="rounded-lg border border-border bg-card p-4">
          <Skeleton className="mb-3 h-2.5 w-20" />
          <Skeleton className="h-7 w-24" />
          <Skeleton className="mt-3 h-7 w-full" />
        </div>
      ))}
    </div>
  )
}

/**
 * Envuelve el contenido de un panel y, según el estado de demo global, muestra
 * skeleton / vacío / error / contenido. Demuestra los estados consistentes (P0).
 */
export function Stateful({
  skeleton,
  empty,
  error,
  errorDetail = "HTTP 500 — el endpoint devolvió un error",
  children,
}: {
  skeleton: ReactNode
  empty: ReactNode
  error?: ReactNode
  errorDetail?: string
  children: ReactNode
}) {
  const { state, setState } = useDemoState()
  if (state === "loading") return <>{skeleton}</>
  if (state === "empty") return <>{empty}</>
  if (state === "error") {
    return <>{error ?? <ErrorState detail={errorDetail} onRetry={() => setState("ready")} />}</>
  }
  return <>{children}</>
}
