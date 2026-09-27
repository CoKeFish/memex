import { useState } from "react"
import { Command as CommandIcon, Menu } from "lucide-react"
import { useLocation } from "react-router-dom"
import { Button } from "@/components/ui/button"
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet"
import { AlertBell } from "./alert-bell"
import { Brand, SidebarNav } from "./sidebar"
import { DemoSwitch } from "./demo-switch"
import { navTitle } from "./nav"
import { RangePicker } from "./range-picker"
import { RefreshControl } from "./refresh-control"
import { ThemeToggle } from "./theme-toggle"

export function TopBar({ onOpenCmdk }: { onOpenCmdk: () => void }) {
  const loc = useLocation()
  const [menuOpen, setMenuOpen] = useState(false)

  return (
    <header className="sticky top-0 z-30 flex h-14 items-center gap-2 border-b border-border bg-background/80 px-3 backdrop-blur md:gap-3 md:px-5">
      <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
        <SheetTrigger asChild>
          <Button variant="ghost" size="icon" className="size-8 md:hidden" aria-label="Menú">
            <Menu className="size-4" />
          </Button>
        </SheetTrigger>
        <SheetContent side="left" className="w-64 p-0">
          <SheetTitle className="sr-only">Navegación</SheetTitle>
          <Brand />
          <div className="border-t border-border" />
          <SidebarNav onNavigate={() => setMenuOpen(false)} />
        </SheetContent>
      </Sheet>

      <div className="min-w-0 flex-1">
        <div className="eyebrow hidden sm:block">memex · consola de debug</div>
        <h1 className="truncate text-sm font-semibold leading-tight">{navTitle(loc.pathname)}</h1>
      </div>

      <button
        onClick={onOpenCmdk}
        className="hidden items-center gap-2 rounded-md border border-border bg-muted/40 px-2.5 py-1.5 text-xs text-muted-foreground transition-colors hover:bg-muted lg:flex"
      >
        <CommandIcon className="size-3.5" /> Buscar
        <kbd className="num rounded border border-border bg-background px-1 text-[10px]">⌘K</kbd>
      </button>

      <RangePicker />
      <div className="hidden md:block">
        <RefreshControl />
      </div>
      <DemoSwitch />
      <AlertBell />
      <ThemeToggle />
    </header>
  )
}
