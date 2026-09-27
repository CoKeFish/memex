import { useState } from "react"
import { Flag, Loader2 } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
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
import { reportFeedback } from "@/data"
import { ApiError } from "@/lib/api"
import type { FeedbackKind, InboxFeedback } from "@/types/domain"

/** Categorías rápidas de feedback (calidad del procesamiento). Solo captura — no resuelve nada. */
const FEEDBACK_OPTIONS: { kind: FeedbackKind; label: string }[] = [
  { kind: "missing_data", label: "No registró todos los datos importantes" },
  { kind: "missed_important", label: "No destacó / notificó algo importante" },
  { kind: "bad_summary", label: "Resumen incorrecto o incompleto" },
  { kind: "wrong_extraction", label: "Extracción incorrecta" },
  { kind: "bad_ocr", label: "OCR / adjunto mal leído" },
  { kind: "other", label: "Otro (ver nota)" },
]

export function FeedbackButton({
  inboxId,
  current,
  onDone,
}: {
  inboxId: number
  current?: InboxFeedback | null
  onDone?: () => void
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [sel, setSel] = useState<Set<FeedbackKind>>(() => new Set(current?.kinds ?? []))
  const [note, setNote] = useState(current?.note ?? "")
  const reported = !!current

  function toggle(kind: FeedbackKind) {
    setSel((prev) => {
      const next = new Set(prev)
      if (next.has(kind)) next.delete(kind)
      else next.add(kind)
      return next
    })
  }

  async function submit() {
    setBusy(true)
    try {
      await reportFeedback(inboxId, { kinds: [...sel], note: note.trim() || null })
      toast.success(`Feedback registrado · #${inboxId}`, {
        description: `${sel.size} categoría(s)`,
      })
      setOpen(false)
      onDone?.()
    } catch (e) {
      toast.error("No se pudo registrar", {
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
          <Flag className={reported ? "size-3.5 text-status-review" : "size-3.5"} />
          {reported ? "Reportado" : "Reportar"}
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Reportar problema · #{inboxId}</DialogTitle>
          <DialogDescription>
            Marcá rápido qué salió mal. Solo se registra (no corrige nada) — sirve para calibrar luego.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1.5">
          {FEEDBACK_OPTIONS.map((o) => (
            <label
              key={o.kind}
              className="flex cursor-pointer items-start gap-3 rounded-md border border-border p-2.5 hover:bg-accent/30"
            >
              <Checkbox checked={sel.has(o.kind)} onCheckedChange={() => toggle(o.kind)} className="mt-0.5" />
              <span className="text-sm">{o.label}</span>
            </label>
          ))}
        </div>
        <Textarea
          placeholder="Nota (opcional): qué faltó / qué esperabas…"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          disabled={busy}
          className="h-20"
        />
        <DialogFooter>
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => setOpen(false)}>
            Cancelar
          </Button>
          <Button size="sm" disabled={busy || sel.size === 0} onClick={submit}>
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Flag className="size-3.5" />}
            Registrar
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
