import { cn } from "@/lib/utils"
import { tierLabel, tierTone, toneText } from "@/lib/status"
import type { Tier } from "@/types/domain"

/** Tag compacto del tier ("el filtro en el que entró": Blacklist / Lote / Individual).
 * Extraído del feed de /datos para reusarlo en el panel del lote de procesamiento. */
export function TierTag({ tier }: { tier: string }) {
  const t = tier as Tier
  return (
    <span
      className={cn(
        "num shrink-0 rounded px-1 py-px text-[9px] font-semibold uppercase tracking-wide",
        toneText[tierTone[t] ?? "neutral"],
      )}
      style={{ backgroundColor: "color-mix(in oklch, currentColor 14%, transparent)" }}
      title={`Clasificación: ${tierLabel[t] ?? tier}`}
    >
      {tierLabel[t] ?? tier}
    </span>
  )
}
