// Acciones de filtro INMEDIATAS sobre un mensaje (no son sugerencias): reasignar tier, bloquear
// remitente (crea una filter_rule activa), y re-clasificar. Cada acción muta estado real al instante.

import { useState } from "react"
import { Ban, Check, Loader2, RotateCw, SlidersHorizontal } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { createFilter, reprocessInboxItem, setClassification } from "@/data"
import { ApiError } from "@/lib/api"
import type { InboxRow } from "@/types/domain"

const TIERS: { value: "blacklist" | "batch" | "individual"; label: string }[] = [
  { value: "individual", label: "Individual (más atención)" },
  { value: "batch", label: "Lote (default)" },
  { value: "blacklist", label: "Blacklist (sin LLM)" },
]

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

export function MessageFilterMenu({
  row,
  sourceType,
  onDone,
}: {
  row: InboxRow
  sourceType: string | null
  onDone?: () => void
}) {
  const [busy, setBusy] = useState(false)
  const tier = row.classification?.tier
  const payload = row.payload as { from?: { email?: string | null } | null }
  const fromEmail = payload?.from?.email ?? null

  async function run(fn: () => Promise<void>, ok: string, okDetail?: string) {
    setBusy(true)
    try {
      await fn()
      toast.success(ok, okDetail ? { description: okDetail } : undefined)
      onDone?.()
    } catch (e) {
      toast.error("No se pudo aplicar", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  const reassign = (t: "blacklist" | "batch" | "individual") =>
    run(
      async () => {
        await setClassification(row.id, t)
      },
      `Tier → ${t}`,
    )

  const blockSender = () =>
    run(
      async () => {
        if (!fromEmail) throw new Error("sin remitente")
        await createFilter({
          sourceType,
          scope: { "from.email": { equals: fromEmail } },
          action: "ignore",
        })
      },
      "Remitente bloqueado",
      fromEmail ? `${fromEmail} — los próximos no se reciben` : undefined,
    )

  const reclassify = () =>
    run(
      async () => {
        await reprocessInboxItem(row.id, ["classify"], true)
      },
      "Re-clasificado",
      "recalculado con las reglas actuales",
    )

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="h-8" disabled={busy}>
          {busy ? <Loader2 className="size-3.5 animate-spin" /> : <SlidersHorizontal className="size-3.5" />}
          Filtros
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-60">
        <DropdownMenuLabel>Reasignar tier</DropdownMenuLabel>
        {TIERS.map((t) => (
          <DropdownMenuItem key={t.value} onSelect={() => reassign(t.value)}>
            {tier === t.value ? <Check className="size-3.5 text-status-ok" /> : <span className="size-3.5" />}
            {t.label}
          </DropdownMenuItem>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={blockSender} disabled={!fromEmail} variant="destructive">
          <Ban className="size-3.5" /> Bloquear remitente
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={reclassify}>
          <RotateCw className="size-3.5" /> Re-clasificar (reglas actuales)
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
