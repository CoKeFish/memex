// Etapas reprocesables (tipo + derivación desde un journey mock). Vive aparte del componente
// para no romper Fast Refresh (react-refresh/only-export-components), igual que llm-trace-runs.

import type { MessageJourney } from "@/types/domain"

/** Una etapa reprocesable. `stage` = nombre del backend (media/ocr/classify/extract). */
export interface ReprocessStep {
  stage: string
  label: string
  hint: string
}

/** Etapas reprocesables derivadas de un journey (mock): cola de revisión y detalle demo. */
export function reprocessStepsFor(j: MessageJourney | null): ReprocessStep[] {
  const out: ReprocessStep[] = [
    { stage: "classify", label: "Re-clasificar", hint: "determinista · sin LLM" },
  ]
  if (!j) return out
  if (j.steps.some((s) => s.kind === "modulo"))
    out.push({ stage: "extract", label: "Re-extraer (módulos)", hint: "LLM · finanzas/calendario" })
  if (j.media.length > 0)
    out.push({ stage: "ocr", label: "Re-OCR de adjuntos", hint: "vuelve a transcribir" })
  return out
}
