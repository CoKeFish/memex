import { Moon, RotateCw, Sun } from "lucide-react"
import { useNavigate } from "react-router-dom"
import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command"
import { useAutoRefresh } from "@/state/auto-refresh"
import { useDemoState } from "@/state/demo-state"
import { useTheme } from "@/state/theme"
import { NAV } from "./nav"

export function CommandPalette({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
}) {
  const nav = useNavigate()
  const { resolved, setTheme } = useTheme()
  const { refreshNow } = useAutoRefresh()
  const { setState } = useDemoState()

  function run(fn: () => void) {
    onOpenChange(false)
    fn()
  }

  return (
    <CommandDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Paleta de comandos"
      description="Navegar y ejecutar acciones"
    >
      <CommandInput placeholder="Buscar vista o acción…" />
      <CommandList>
        <CommandEmpty>Sin resultados.</CommandEmpty>
        <CommandGroup heading="Ir a">
          {NAV.map((n) => (
            <CommandItem key={n.path} value={`ir ${n.label}`} onSelect={() => run(() => nav(n.path))}>
              <n.icon className="size-4" />
              {n.label}
              {n.stub && <span className="eyebrow ml-auto">stub</span>}
            </CommandItem>
          ))}
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading="Acciones">
          <CommandItem value="refrescar datos" onSelect={() => run(refreshNow)}>
            <RotateCw className="size-4" /> Refrescar datos
          </CommandItem>
          <CommandItem
            value="cambiar tema"
            onSelect={() => run(() => setTheme(resolved === "dark" ? "light" : "dark"))}
          >
            {resolved === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />} Cambiar tema
          </CommandItem>
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading="Simular estado (demo)">
          <CommandItem value="estado datos normales" onSelect={() => run(() => setState("ready"))}>
            Datos normales
          </CommandItem>
          <CommandItem value="estado cargando" onSelect={() => run(() => setState("loading"))}>
            Cargando (skeleton)
          </CommandItem>
          <CommandItem value="estado vacio" onSelect={() => run(() => setState("empty"))}>
            Vacío
          </CommandItem>
          <CommandItem value="estado error" onSelect={() => run(() => setState("error"))}>
            Error
          </CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  )
}
