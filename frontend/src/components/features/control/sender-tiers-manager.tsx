// Gestión de overrides de tier por remitente (sender_tier_overrides): el filtro de PROCESAMIENTO.
// Fuerza el tier de los mensajes FUTUROS de un remitente — blacklist = se guarda sin gasto LLM,
// individual = atención 1 a 1. Prospectivo: el classifier lo consulta antes de la heurística.

import { useState } from "react"
import { Loader2, Plus, Trash2 } from "lucide-react"
import { toast } from "sonner"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { TierTag } from "@/components/common/tier-tag"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { clearSenderTier, fetchSenderTiers, setSenderTier } from "@/data"
import type { SenderTierOverride } from "@/data"
import { ApiError } from "@/lib/api"
import { tierLabel } from "@/lib/status"
import { useAsync } from "@/lib/use-async"
import type { Tier } from "@/types/domain"

const TIER_OPTIONS: { value: Tier; hint: string }[] = [
  { value: "blacklist", hint: "se guarda, sin gasto LLM" },
  { value: "batch", hint: "resumen en lote (default de la heurística)" },
  { value: "individual", hint: "atención 1 a 1" },
]

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

/** Fecha corta `YYYY-MM-DD` desde un ISO. */
function shortDate(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "—"
}

export function SenderTiersManager() {
  const { data: rows, loading, error, reload } = useAsync<SenderTierOverride[]>(
    () => fetchSenderTiers(),
    [],
  )
  const [busy, setBusy] = useState(false)

  // Form de alta.
  const [email, setEmail] = useState("")
  const [tier, setTier] = useState<Tier>("blacklist")
  const [reason, setReason] = useState("")

  async function mutate(fn: () => Promise<void>, ok: string, okDetail?: string) {
    setBusy(true)
    try {
      await fn()
      toast.success(ok, okDetail ? { description: okDetail } : undefined)
      reload()
    } catch (e) {
      toast.error("No se pudo aplicar", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  const create = () =>
    mutate(
      async () => {
        await setSenderTier(email.trim().toLowerCase(), tier, reason.trim() || null)
        setEmail("")
        setReason("")
      },
      "Override aplicado",
      `${email.trim().toLowerCase()} → ${tierLabel[tier]} (mensajes futuros)`,
    )

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="filtros · post-ingest"
        title="Tier por remitente"
        sub="fuerza el tier de los mensajes futuros (blacklist = se guarda sin gasto LLM); gana a la heurística, no re-clasifica lo recibido"
      />
      <PanelBody className="space-y-4">
        {/* Nuevo override */}
        <div className="grid gap-2 rounded-md border border-border bg-muted/20 p-3 sm:grid-cols-[1fr_auto_auto]">
          <div className="space-y-2">
            <Input
              placeholder="remitente (email exacto; p. ej. promos@tienda.com)"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="h-8"
            />
            <Input
              placeholder="motivo (opcional)"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="h-8"
            />
          </div>
          <Select value={tier} onValueChange={(v) => setTier(v as Tier)}>
            <SelectTrigger className="h-8 w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {TIER_OPTIONS.map((t) => (
                <SelectItem key={t.value} value={t.value}>
                  {tierLabel[t.value]} — {t.hint}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button size="sm" disabled={busy || !email.trim()} onClick={create} className="self-start">
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
            Aplicar
          </Button>
        </div>

        {/* Lista de overrides activos */}
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !rows ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Cargando overrides…
          </div>
        ) : !rows || rows.length === 0 ? (
          <EmptyState
            title="Sin overrides"
            hint="Forzá el tier de un remitente acá, desde Relevancia o desde el detalle de un mensaje."
          />
        ) : (
          <div className="divide-y divide-border rounded-md border border-border">
            {rows.map((r) => (
              <div key={r.senderEmail} className="flex flex-wrap items-center gap-2 px-3 py-2">
                <TierTag tier={r.tier} />
                <span className="num min-w-0 text-xs font-medium">{r.senderEmail}</span>
                {r.reason && (
                  <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground" title={r.reason}>
                    {r.reason}
                  </span>
                )}
                <span className="num ml-auto text-[11px] text-muted-foreground" title="último cambio">
                  {shortDate(r.updatedAt)}
                </span>
                <Select
                  value={r.tier}
                  onValueChange={(v) =>
                    void mutate(
                      () => setSenderTier(r.senderEmail, v as Tier, r.reason),
                      `Tier → ${tierLabel[v as Tier]}`,
                    )
                  }
                >
                  <SelectTrigger className="h-7 w-32 text-xs" disabled={busy}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {TIER_OPTIONS.map((t) => (
                      <SelectItem key={t.value} value={t.value}>
                        {tierLabel[t.value]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <Button
                  variant="ghost"
                  size="icon"
                  disabled={busy}
                  onClick={() =>
                    void mutate(
                      () => clearSenderTier(r.senderEmail),
                      "Override quitado",
                      `${r.senderEmail} vuelve a la heurística`,
                    )
                  }
                  title="Quitar (vuelve a la heurística)"
                >
                  <Trash2 className="size-3.5 text-status-error" />
                </Button>
              </div>
            ))}
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}
