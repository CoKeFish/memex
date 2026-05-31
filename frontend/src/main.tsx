import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import { BrowserRouter } from "react-router-dom"
import "./index.css"
import App from "./App"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { AlertsProvider } from "@/state/alerts"
import { AutoRefreshProvider } from "@/state/auto-refresh"
import { DemoStateProvider } from "@/state/demo-state"
import { ThemeProvider } from "@/state/theme"
import { TimeRangeProvider } from "@/state/time-range"

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <ThemeProvider>
        <AutoRefreshProvider>
          <DemoStateProvider>
            <AlertsProvider>
              <TimeRangeProvider>
                <TooltipProvider delayDuration={150}>
                  <App />
                  <Toaster position="bottom-right" richColors closeButton />
                </TooltipProvider>
              </TimeRangeProvider>
            </AlertsProvider>
          </DemoStateProvider>
        </AutoRefreshProvider>
      </ThemeProvider>
    </BrowserRouter>
  </StrictMode>,
)
