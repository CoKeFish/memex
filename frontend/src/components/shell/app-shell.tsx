import { useEffect, useState } from "react"
import { Outlet, useLocation } from "react-router-dom"
import { ErrorBoundary } from "@/components/common/error-boundary"
import { CommandPalette } from "./command-palette"
import { Sidebar } from "./sidebar"
import { TopBar } from "./topbar"

export function AppShell() {
  const [cmdkOpen, setCmdkOpen] = useState(false)
  const location = useLocation()

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault()
        setCmdkOpen((o) => !o)
      }
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [])

  return (
    <div className="flex h-svh overflow-hidden">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar onOpenCmdk={() => setCmdkOpen(true)} />
        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[1400px] p-4 md:p-6">
            <ErrorBoundary key={location.pathname}>
              <Outlet />
            </ErrorBoundary>
          </div>
        </main>
      </div>
      <CommandPalette open={cmdkOpen} onOpenChange={setCmdkOpen} />
    </div>
  )
}
