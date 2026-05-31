import { useState } from "react"
import { RotateCw } from "lucide-react"
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
import { CapBadge } from "@/components/common/cap-badge"

export interface ReprocessStep {
  key: string
  label: string
  cursor: string
  cost: string
}

export function ReprocessButton({ inboxId, steps }: { inboxId: number; steps: ReprocessStep[] }) {
  const [open, setOpen] = useState(false)
  const [sel, setSel] = useState<Record<string, boolean>>(() => Object.fromEntries(steps.map((s) => [s.key, true])))
  const chosen = steps.filter((s) => sel[s.key])

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" className="h-8">
          <RotateCw className="size-3.5" /> Reprocesar
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Reprocesar mensaje #{inboxId}</DialogTitle>
          <DialogDescription>
            Elegí qué etapas re-ejecutar. Cada una invalida su cursor (se descarta la fila previa) y vuelve a correr — puede costar LLM.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          {steps.map((s) => (
            <label key={s.key} className="flex cursor-pointer items-start gap-3 rounded-md border border-border p-2.5 hover:bg-accent/30">
              <Checkbox checked={sel[s.key]} onCheckedChange={(c) => setSel((p) => ({ ...p, [s.key]: c === true }))} className="mt-0.5" />
              <div className="min-w-0 flex-1">
                <div className="text-sm font-medium">{s.label}</div>
                <div className="num text-[11px] text-muted-foreground">
                  cursor: {s.cursor} · {s.cost}
                </div>
              </div>
            </label>
          ))}
        </div>
        <div className="flex items-center gap-2 rounded-md border border-status-review/30 bg-status-review/10 px-3 py-2 text-[11px] text-status-review">
          <CapBadge level="parcial" />
          Reproceso vía CLI (re-extract / re-clasificar / backfill); endpoint HTTP es futuro.
        </div>
        <DialogFooter>
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
            Cancelar
          </Button>
          <Button
            size="sm"
            disabled={chosen.length === 0}
            onClick={() => {
              setOpen(false)
              toast.success(`Reprocesando mensaje #${inboxId}`, {
                description: `${chosen.length} paso(s) encolados: ${chosen.map((s) => s.label).join(", ")}. Se invalidan los cursores correspondientes.`,
              })
            }}
          >
            <RotateCw className="size-3.5" /> Reprocesar {chosen.length} paso(s)
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
