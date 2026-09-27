import { FlaskConical } from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { useDemoState, type DemoState } from "@/state/demo-state"

const OPTS: { v: DemoState; label: string }[] = [
  { v: "ready", label: "Datos" },
  { v: "loading", label: "Cargando" },
  { v: "empty", label: "Vacío" },
  { v: "error", label: "Error" },
]

export function DemoSwitch() {
  const { state, setState } = useDemoState()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className={cn("size-8", state !== "ready" && "text-brand")}
          aria-label="Simular estado"
          title="Simular estado de las vistas (vacío / carga / error)"
        >
          <FlaskConical className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel className="eyebrow">Simular estado</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuRadioGroup value={state} onValueChange={(v) => setState(v as DemoState)}>
          {OPTS.map((o) => (
            <DropdownMenuRadioItem key={o.v} value={o.v}>
              {o.label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
