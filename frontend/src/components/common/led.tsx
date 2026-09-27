import { cn } from "@/lib/utils"
import { toneText, type Tone } from "@/lib/status"

/** Punto LED con halo — combinar con un tono de estado. */
export function Led({
  tone,
  pulse = false,
  size = 8,
  className,
}: {
  tone: Tone
  pulse?: boolean
  size?: number
  className?: string
}) {
  return (
    <span
      aria-hidden
      style={{ width: size, height: size }}
      className={cn("led shrink-0", toneText[tone], pulse && "led-pulse", className)}
    />
  )
}

/** Badge de estado tipo etiqueta de instrumento: LED + texto mono en mayúscula. */
export function StatusBadge({
  tone,
  label,
  pulse = false,
  className,
}: {
  tone: Tone
  label: string
  pulse?: boolean
  className?: string
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-1.5 py-0.5 font-mono text-[10px] font-medium uppercase tracking-wider",
        toneText[tone],
        className,
      )}
      style={{
        backgroundColor: "color-mix(in oklch, currentColor 12%, transparent)",
        borderColor: "color-mix(in oklch, currentColor 32%, transparent)",
      }}
    >
      <span className={cn("led", pulse && "led-pulse")} style={{ width: 6, height: 6 }} />
      {label}
    </span>
  )
}
