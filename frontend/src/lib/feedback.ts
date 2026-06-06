// Etiquetas legibles para el feedback manual (categorías + estado). Fuente única para la vista
// /calidad y el botón de reporte. Mismo patrón Record<Enum,string> que `lib/status.ts`.

import type { FeedbackKind } from "@/types/domain"

export const FEEDBACK_KIND_LABEL: Record<FeedbackKind, string> = {
  missing_data: "No registró todos los datos importantes",
  missed_important: "No destacó / notificó algo importante",
  bad_summary: "Resumen incorrecto o incompleto",
  wrong_extraction: "Extracción incorrecta",
  bad_ocr: "OCR / adjunto mal leído",
  other: "Otro",
}

export const FEEDBACK_STATUS_LABEL: Record<"open" | "reviewed" | "dismissed", string> = {
  open: "Abierto",
  reviewed: "Revisado",
  dismissed: "Descartado",
}
