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
import { CalendarPage } from "@/pages/calendar"
import { IngestPage } from "@/pages/ingest"
import { ProcessingPage } from "@/pages/processing"
import { FiltersPage } from "@/pages/filters"
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
        <Route path="finanzas" element={<FinancePage />} />
        <Route path="metricas" element={<MetricsPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="cuenta" element={<AccountPage />} />
        <Route path="carga" element={<IngestPage />} />
        <Route path="filtros" element={<FiltersPage />} />
        <Route
          path="ocr"
          element={
            <StubView
              eyebrow="Categoría · multimedia"
              title="Multimedia / OCR"
              description="Pipeline de OCR sobre imágenes en MinIO, visor texto-vs-imagen y reproceso."
              features={[
                "Monitor del pipeline de OCR/Media con re-OCR",
                "Visor OCR: texto extraído vs imagen original",
                "Señalización de texto OCR truncado o incierto",
                "Piso de tamaño de OCR (anti tracking-pixel)",
              ]}
            />
          }
        />
        <Route
          path="calidad"
          element={
            <StubView
              eyebrow="Categoría · calidad"
              title="Calidad y precisión de los datos"
              description="Procedencia, evidencia y confianza: cada dato rastreable a su mensaje de origen."
              features={[
                "Drill-down de procedencia con resaltado de evidencia",
                "Badge de confianza/decisión del LLM por extracción",
                "Detección de huecos de cobertura",
                "Historial de decisiones de dedup",
                "Inspección de duplicados de ingesta",
              ]}
            />
          }
        />
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
