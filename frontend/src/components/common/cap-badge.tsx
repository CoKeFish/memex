import { StatusBadge } from "./led"
import type { Tone } from "@/lib/status"

export type CapLevel = "existe" | "parcial" | "futuro"

const MAP: Record<CapLevel, { tone: Tone; label: string }> = {
  existe: { tone: "ok", label: "existe" },
  parcial: { tone: "review", label: "parcial" },
  futuro: { tone: "neutral", label: "futuro" },
}

/** Marca qué tan real es un control hoy: existe (CLI/columna) · parcial · futuro (requiere backend). */
export function CapBadge({ level, title }: { level: CapLevel; title?: string }) {
  const m = MAP[level]
  return (
    <span title={title}>
      <StatusBadge tone={m.tone} label={m.label} />
    </span>
  )
}
