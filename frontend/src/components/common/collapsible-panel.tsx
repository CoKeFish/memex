import { useState, type ReactNode } from "react"
import { ChevronDown } from "lucide-react"
import { cn } from "@/lib/utils"
import { Panel, PanelBody } from "@/components/common/panel"

/** Panel cuyo header es un botón que abre/cierra el cuerpo. Mantiene la firma visual del Panel
 * (corner-ticks) y el mismo header (eyebrow + título + sub) que PanelHeader, con un chevron que rota.
 * Reusa el patrón de toggle de message/llm-trace.tsx. Cerrado por defecto. */
export function CollapsiblePanel({
  eyebrow,
  title,
  sub,
  right,
  defaultOpen = false,
  bodyClassName,
  children,
}: {
  eyebrow?: ReactNode
  title: ReactNode
  sub?: ReactNode
  /** Contenido no interactivo a la derecha del header (p. ej. un badge). */
  right?: ReactNode
  defaultOpen?: boolean
  bodyClassName?: string
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <Panel>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className={cn(
          "flex w-full items-start justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-muted/20",
          open && "border-b border-border",
        )}
      >
        <div className="flex min-w-0 items-start gap-2.5">
          <ChevronDown
            className={cn(
              "mt-0.5 size-4 shrink-0 text-muted-foreground transition-transform",
              !open && "-rotate-90",
            )}
          />
          <div className="min-w-0">
            {eyebrow && <div className="eyebrow mb-1.5">{eyebrow}</div>}
            <h2 className="truncate text-sm font-semibold leading-tight text-foreground">{title}</h2>
            {sub && <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p>}
          </div>
        </div>
        {right && <div className="shrink-0">{right}</div>}
      </button>
      {open && <PanelBody className={bodyClassName}>{children}</PanelBody>}
    </Panel>
  )
}
