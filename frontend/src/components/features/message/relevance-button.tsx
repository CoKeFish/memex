import { useState } from "react"
import { Gauge, Loader2 } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { clearRelevanceMark, setRelevanceMark } from "@/data"
import { ApiError } from "@/lib/api"
import type { RelevanceMark } from "@/types/domain"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

/**
 * Marca manual de relevancia de un mensaje (override por-mensaje; gana sobre la heurística para ESTE
 * mensaje y alimenta la métrica de /relevancia). NO bloquea al remitente ni toca filtros. Marcar uno
 * no condena a todos sus mensajes.
 */
export function RelevanceButton({
  inboxId,
  current,
  onDone,
}: {
  inboxId: number
  current?: RelevanceMark | null
  onDone?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [reason, setReason] = useState(current?.reason ?? "")

  const triggerLabel = !current ? "Relevancia" : current.isRelevant ? "Relevante" : "No relevante"
  const triggerTone = !current
    ? "size-3.5"
    : current.isRelevant
      ? "size-3.5 text-status-ok"
      : "size-3.5 text-status-review"

  async function act(fn: () => Promise<unknown>, msg: string) {
    setBusy(true)
    try {
      await fn()
      toast.success(msg)
      setOpen(false)
      onDone?.()
    } catch (e) {
      toast.error("No se pudo", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  const mark = (isRelevant: boolean) =>
    act(
      () => setRelevanceMark(inboxId, isRelevant, reason.trim() || null),
      `Marcado ${isRelevant ? "relevante" : "no relevante"} · #${inboxId}`,
    )
  const clear = () => act(() => clearRelevanceMark(inboxId), `Marca quitada · #${inboxId}`)

  return (
    <Dialog open={open} onOpenChange={(o) => !busy && setOpen(o)}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" className="h-8">
          <Gauge className={triggerTone} />
          {triggerLabel}
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Relevancia del mensaje · #{inboxId}</DialogTitle>
          <DialogDescription>
            Tu marca gana sobre la heurística para ESTE mensaje y alimenta la métrica por remitente.
            No bloquea al remitente ni toca filtros; marcar uno no condena a todos sus mensajes.
          </DialogDescription>
        </DialogHeader>
        <Textarea
          placeholder="Motivo (opcional)…"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          disabled={busy}
          className="h-20"
        />
        <DialogFooter className="sm:justify-between">
          <div>
            {current && (
              <Button variant="ghost" size="sm" disabled={busy} onClick={clear}>
                Quitar marca
              </Button>
            )}
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={busy} onClick={() => mark(false)}>
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : null}
              No es relevante
            </Button>
            <Button size="sm" disabled={busy} onClick={() => mark(true)}>
              Es relevante
            </Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
