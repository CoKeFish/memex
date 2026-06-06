import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { Led } from "@/components/common/led"
import type { Tone } from "@/lib/status"
import { cn } from "@/lib/utils"
import type { BienestarHabit } from "@/data/bienestar"

/** Tono del LED de la racha: cumplido hoy = ok; racha viva pero período en curso pendiente =
 *  pending (la gracia no la rompió aún); sin racha = error. */
function streakTone(h: BienestarHabit): Tone {
  if (h.metCurrent) return "ok"
  if (h.streak > 0) return "pending"
  return "error"
}

export function HabitsPanel({ habits }: { habits: BienestarHabit[] }) {
  return (
    <Panel>
      <PanelHeader eyebrow="hábitos" title="Adherencia" sub="racha y cumplimiento por período" />
      <PanelBody className="space-y-4">
        {habits.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Sin hábitos. Definí uno con{" "}
            <code className="num text-xs">memex-bienestar habit add</code>.
          </p>
        ) : (
          habits.map((h) => (
            <div key={h.id} className="space-y-1.5">
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <Led tone={streakTone(h)} />
                  <span className="truncate text-sm font-medium text-foreground">{h.name}</span>
                  <span className="num shrink-0 text-xs text-muted-foreground">
                    {h.cadence === "weekly" ? "semanal" : "diario"} · {h.current}/{h.targetCount}
                  </span>
                </div>
                <span className="num shrink-0 text-xs text-muted-foreground">racha {h.streak}</span>
              </div>
              <div className="flex flex-wrap gap-1">
                {h.history.map((p) => (
                  <span
                    key={p.period}
                    title={`${p.period}: ${p.count}`}
                    className={cn(
                      "h-3 w-3 rounded-[3px] border",
                      p.met ? "border-status-ok/40 bg-status-ok/70" : "border-border bg-muted/40",
                    )}
                  />
                ))}
              </div>
            </div>
          ))
        )}
      </PanelBody>
    </Panel>
  )
}
