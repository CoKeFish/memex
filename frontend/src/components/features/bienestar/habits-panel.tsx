import { useState } from "react"
import { Loader2, Plus, Trash2 } from "lucide-react"
import { toast } from "sonner"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { Led } from "@/components/common/led"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { createBienestarHabit, deleteBienestarHabit } from "@/data"
import { ApiError } from "@/lib/api"
import type { Tone } from "@/lib/status"
import { cn } from "@/lib/utils"
import type { BienestarHabit } from "@/data/bienestar"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

/** Tono del LED de la racha: cumplido hoy = ok; racha viva pero período en curso pendiente =
 *  pending (la gracia no la rompió aún); sin racha = error. */
function streakTone(h: BienestarHabit): Tone {
  if (h.metCurrent) return "ok"
  if (h.streak > 0) return "pending"
  return "error"
}

export function HabitsPanel({
  habits,
  onChanged,
}: {
  habits: BienestarHabit[]
  onChanged: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState("")
  const [activity, setActivity] = useState("")
  const [cadence, setCadence] = useState<"daily" | "weekly">("daily")
  const [target, setTarget] = useState("1")

  async function create() {
    setBusy(true)
    try {
      await createBienestarHabit({
        name: name.trim(),
        activity: activity.trim(),
        cadence,
        targetCount: Math.max(1, Number(target) || 1),
      })
      toast.success("Hábito creado")
      setName("")
      setActivity("")
      setTarget("1")
      onChanged()
    } catch (e) {
      toast.error("No se pudo crear", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: number, label: string) {
    setBusy(true)
    try {
      await deleteBienestarHabit(id)
      toast.success(`Hábito "${label}" borrado`)
      onChanged()
    } catch (e) {
      toast.error("No se pudo borrar", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel>
      <PanelHeader eyebrow="hábitos" title="Adherencia" sub="racha y cumplimiento por período" />
      <PanelBody className="space-y-4">
        {/* Nuevo hábito */}
        <div className="grid gap-2 rounded-md border border-border bg-muted/20 p-3 sm:grid-cols-[1fr_1fr_auto_auto_auto]">
          <Input
            placeholder="nombre (ej. Gimnasio)"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="h-8"
          />
          <Input
            placeholder="actividad (ej. gimnasio)"
            value={activity}
            onChange={(e) => setActivity(e.target.value)}
            className="h-8"
            title="el acto que cuenta (debe coincidir con la --activity de los registros)"
          />
          <Select value={cadence} onValueChange={(v) => setCadence(v as "daily" | "weekly")}>
            <SelectTrigger className="h-8 w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="daily">diario</SelectItem>
              <SelectItem value="weekly">semanal</SelectItem>
            </SelectContent>
          </Select>
          <Input
            type="number"
            min={1}
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            className="h-8 w-16"
            title="meta por período"
          />
          <Button size="sm" disabled={busy || !name.trim() || !activity.trim()} onClick={create}>
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
            Agregar
          </Button>
        </div>

        {/* Lista */}
        {habits.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Sin hábitos todavía. Creá uno arriba (nombre + actividad + cadencia + meta).
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
                <div className="flex shrink-0 items-center gap-1.5">
                  <span className="num text-xs text-muted-foreground">racha {h.streak}</span>
                  <Button
                    variant="ghost"
                    size="icon"
                    disabled={busy}
                    onClick={() => remove(h.id, h.name)}
                    title="Borrar hábito"
                  >
                    <Trash2 className="size-3.5 text-status-error" />
                  </Button>
                </div>
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
