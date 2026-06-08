import type { ReactNode } from "react"
import { Navigate, Route, Routes } from "react-router-dom"
import { AppShell } from "@/components/shell/app-shell"
import { LoginPage } from "@/pages/login"
import { SessionProvider, useSession } from "@/state/session"
import { OverviewPage } from "@/pages/overview"
import { PipelinePage } from "@/pages/pipeline"
import { ReviewPage } from "@/pages/review"
import { DataPage } from "@/pages/data"
import { MessageDetailPage } from "@/pages/message-detail"
import { MetricsPage } from "@/pages/metrics"
import { LogsPage } from "@/pages/logs"
import { AccountPage } from "@/pages/account"
import { FinancePage } from "@/pages/finance"
import { BienestarPage } from "@/pages/bienestar"
import { HackathonesPage } from "@/pages/hackathones"
import { CalendarPage } from "@/pages/calendar"
import { IdentidadesPage } from "@/pages/identidades"
import { GraphPage } from "@/pages/graph"
import { IngestPage } from "@/pages/ingest"
import { ProcessingPage } from "@/pages/processing"
import { FiltersPage } from "@/pages/filters"
import { QualityPage } from "@/pages/quality"
import { SenderRelevancePage } from "@/pages/sender-relevance"
import { OcrMediaPage } from "@/pages/ocr-media"
import { StubView } from "@/pages/stub"

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useSession()
  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">
        Cargando…
      </div>
    )
  }
  if (!user) return <Navigate to="/login" replace />
  return <>{children}</>
}

export default function App() {
  return (
    <SessionProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
        <Route index element={<OverviewPage />} />
        <Route path="pipeline" element={<PipelinePage />} />
        <Route path="revision" element={<ReviewPage />} />
        <Route path="datos" element={<DataPage />} />
        <Route path="datos/:id" element={<MessageDetailPage />} />
        <Route path="calendario" element={<CalendarPage />} />
        <Route path="directorio" element={<IdentidadesPage />} />
        <Route path="grafo" element={<GraphPage />} />
        <Route path="finanzas" element={<FinancePage />} />
        <Route path="bienestar" element={<BienestarPage />} />
        <Route path="hackathones" element={<HackathonesPage />} />
        <Route path="metricas" element={<MetricsPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="cuenta" element={<AccountPage />} />
        <Route path="carga" element={<IngestPage />} />
        <Route path="filtros" element={<FiltersPage />} />
        <Route path="ocr" element={<OcrMediaPage />} />
        <Route path="calidad" element={<QualityPage />} />
        <Route path="relevancia" element={<SenderRelevancePage />} />
        <Route path="procesamiento" element={<ProcessingPage />} />
        <Route
          path="*"
          element={<StubView eyebrow="404" title="Vista no encontrada" description="La ruta no existe." features={[]} />}
        />
        </Route>
      </Routes>
    </SessionProvider>
  )
}
