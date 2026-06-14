import { useState } from "react"
import { Loader2, RotateCw } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { reprocessInboxItem, type ReprocessResult } from "@/data"
import { ApiError } from "@/lib/api"
import type { ReprocessStep } from "./reprocess-steps"

const n = (v: unknown): number => Number(v ?? 0)

/** Resumen corto del resultado por etapa para el toast. */
function summarize(res: ReprocessResult): string {
  return Object.entries(res.results)
    .map(([stage, r]) => {
      if ("error" in r) return `${stage}: error`
      if (stage === "media") return `adjuntos +${n(r.assets_created)}`
      if (stage === "ocr") return `OCR ${n(r.ok)} ok`
      if (stage === "classify") return `clasif ${n(r.classified)}`
      if (stage === "extract") return `extracción ${n(r.items)} item(s)`
      return stage
    })
    .join(" · ")
}

export function ReprocessButton({
  inboxId,
  steps,
  onDone,
}: {
  inboxId: number
  steps: ReprocessStep[]
  onDone?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [force, setForce] = useState(true)
  const [sel, setSel] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(steps.map((s) => [s.stage, true])),
  )
  const chosen = steps.filter((s) => sel[s.stage])

  async function run() {
    setBusy(true)
    try {
      const res = await reprocessInboxItem(
        inboxId,
        chosen.map((s) => s.stage),
        force,
      )
      toast.success(`Reprocesado #${inboxId}`, { description: summarize(res) || "sin cambios" })
      setOpen(false)
      onDone?.()
    } catch (e) {
      toast.error("No se pudo reprocesar", {
        description: e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e),
      })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !busy && setOpen(o)}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" className="h-8">
          <RotateCw className="size-3.5" /> Reprocesar
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Reprocesar mensaje #{inboxId}</DialogTitle>
          <DialogDescription>
            Elegí qué etapas re-ejecutar. Corren en orden de dependencia (adjuntos → OCR →
            clasificar → extraer). Puede costar LLM/OCR.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          {steps.map((s) => (
            <label
              key={s.stage}
              className="flex cursor-pointer items-start gap-3 rounded-md border border-border p-2.5 hover:bg-accent/30"
            >
              <Checkbox
                checked={sel[s.stage]}
                onCheckedChange={(c) => setSel((p) => ({ ...p, [s.stage]: c === true }))}
                className="mt-0.5"
              />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium">{s.label}</div>
                <div className="num text-[11px] text-muted-foreground">{s.hint}</div>
              </div>
            </label>
          ))}
        </div>
        <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
          <Checkbox checked={force} onCheckedChange={(c) => setForce(c === true)} />
          Forzar — reprocesar lo ya hecho (invalida cursores / re-OCR). Sin esto, salta lo completado.
        </label>
        <DialogFooter>
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => setOpen(false)}>
            Cancelar
          </Button>
          <Button size="sm" disabled={busy || chosen.length === 0} onClick={run}>
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : <RotateCw className="size-3.5" />}
            Reprocesar {chosen.length} paso(s)
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
