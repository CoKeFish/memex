import { Route, Routes } from "react-router-dom"
import { AppShell } from "@/components/shell/app-shell"
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
import { StubView } from "@/pages/stub"

export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
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
        <Route
          path="carga"
          element={
            <StubView
              eyebrow="Categoría · carga manual"
              title="Carga manual y acciones"
              description="Alta manual de eventos, requeue, ingesta puntual y correcciones sobre los datos."
              features={[
                "Alta manual de evento (alta prioridad, protegido)",
                "Ingesta puntual ad-hoc con dry-run obligatorio",
                "Editar/corregir un gasto extraído",
                "Marcar importante / promover tier (batch → individual)",
                "Aprobar / rechazar propuestas de reglas",
                "Confirmación + Undo transversal",
              ]}
            />
          }
        />
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
        <Route
          path="procesamiento"
          element={
            <StubView
              eyebrow="Categoría · procesamiento"
              title="Controles de procesamiento y reproceso"
              description="Perillas de batch tuning, administración de módulos, triggers y reproceso/backfill."
              features={[
                "Panel de perillas de batch tuning con estimación de costo",
                "Administración de módulos: habilitar + política de batching",
                "Gestor de cuentas de proveedor de calendar + write-back",
                "Barra de comando de corrida con dry-run + scheduler",
                "Editor de filter_rules con simulación pre-ingest",
                "Reprocesar: re-extraer / re-clasificar / backfill",
              ]}
            />
          }
        />
        <Route
          path="*"
          element={<StubView eyebrow="404" title="Vista no encontrada" description="La ruta no existe." features={[]} />}
        />
      </Route>
    </Routes>
  )
}
