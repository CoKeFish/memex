import { Monitor, Moon, Sun } from "lucide-react"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { useTheme, type Theme } from "@/state/theme"

const OPTS: { v: Theme; label: string; Icon: typeof Sun }[] = [
  { v: "light", label: "Claro", Icon: Sun },
  { v: "dark", label: "Oscuro", Icon: Moon },
  { v: "system", label: "Sistema", Icon: Monitor },
]

export function ThemeToggle() {
  const { theme, resolved, setTheme } = useTheme()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" className="size-8" aria-label="Tema">
          {resolved === "dark" ? <Moon className="size-4" /> : <Sun className="size-4" />}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {OPTS.map(({ v, label, Icon }) => (
          <DropdownMenuItem key={v} onClick={() => setTheme(v)} className={cn(theme === v && "text-brand")}>
            <Icon className="size-4" />
            {label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
