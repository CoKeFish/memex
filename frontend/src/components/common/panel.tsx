import type { ReactNode } from "react"
import { cn } from "@/lib/utils"

/** Marcas de registro en las esquinas — firma visual de "instrumento". */
function CornerTicks() {
  const base = "pointer-events-none absolute h-2 w-2 border-brand/40"
  return (
    <>
      <span className={cn(base, "left-0 top-0 rounded-tl-lg border-l border-t")} />
      <span className={cn(base, "right-0 top-0 rounded-tr-lg border-r border-t")} />
      <span className={cn(base, "bottom-0 left-0 rounded-bl-lg border-b border-l")} />
      <span className={cn(base, "bottom-0 right-0 rounded-br-lg border-b border-r")} />
    </>
  )
}

export function Panel({
  children,
  className,
  ticks = true,
}: {
  children: ReactNode
  className?: string
  ticks?: boolean
}) {
  return (
    <section className={cn("relative rounded-lg border border-border bg-card", className)}>
      {ticks && <CornerTicks />}
      {children}
    </section>
  )
}

export function PanelHeader({
  eyebrow,
  title,
  sub,
  right,
}: {
  eyebrow?: ReactNode
  title: ReactNode
  sub?: ReactNode
  right?: ReactNode
}) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
      <div className="min-w-0">
        {eyebrow && <div className="eyebrow mb-1.5">{eyebrow}</div>}
        <h2 className="truncate text-sm font-semibold leading-tight text-foreground">{title}</h2>
        {sub && <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p>}
      </div>
      {right && <div className="shrink-0">{right}</div>}
    </div>
  )
}

export function PanelBody({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("p-4", className)}>{children}</div>
}

export function Eyebrow({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cn("eyebrow", className)}>{children}</span>
}
