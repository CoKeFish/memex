// Gestión de reglas de filtro pre-ingest (filter_rules) desde el dashboard. Antes solo CLI.
// Las reglas con action=ignore descartan ANTES de ingestar (bloquean los próximos, no los recibidos).

import { useState } from "react"
import { Loader2, Plus, Trash2 } from "lucide-react"
import { toast } from "sonner"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { createFilter, deleteFilter, fetchFilters, updateFilter } from "@/data"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import type { FilterAction, FilterRule } from "@/types/domain"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

const SCOPE_PLACEHOLDER = '{"from.email": {"equals": "spam@x.com"}}'

export function FiltersManager() {
  const { data: rules, loading, error, reload } = useAsync<FilterRule[]>(() => fetchFilters(), [])
  const [busy, setBusy] = useState(false)

  // Form de nueva regla.
  const [sourceType, setSourceType] = useState("")
  const [scopeText, setScopeText] = useState("")
  const [action, setAction] = useState<FilterAction>("ignore")
  const [priority, setPriority] = useState("100")

  async function create() {
    let scope: Record<string, unknown>
    try {
      scope = JSON.parse(scopeText || "{}")
      if (typeof scope !== "object" || Array.isArray(scope)) throw new Error("scope debe ser un objeto")
    } catch (e) {
      toast.error("Scope JSON inválido", { description: e instanceof Error ? e.message : String(e) })
      return
    }
    setBusy(true)
    try {
      await createFilter({
        sourceType: sourceType.trim() || null,
        scope,
        action,
        priority: Number(priority) || 100,
      })
      toast.success("Regla creada")
      setScopeText("")
      setSourceType("")
      reload()
    } catch (e) {
      toast.error("No se pudo crear", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  async function mutate(fn: () => Promise<void>, ok: string) {
    setBusy(true)
    try {
      await fn()
      toast.success(ok)
      reload()
    } catch (e) {
      toast.error("No se pudo aplicar", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="filtros · pre-ingest"
        title="Reglas de filtro"
        sub="action=ignore descarta antes de ingestar (corta los próximos, no los ya recibidos)"
      />
      <PanelBody className="space-y-4">
        {/* Nueva regla */}
        <div className="grid gap-2 rounded-md border border-border bg-muted/20 p-3 sm:grid-cols-[1fr_auto_auto]">
          <div className="space-y-2">
            <Input
              placeholder="source_type (vacío = todas; p. ej. imap)"
              value={sourceType}
              onChange={(e) => setSourceType(e.target.value)}
              className="h-8"
            />
            <Textarea
              placeholder={`scope JSON · ${SCOPE_PLACEHOLDER}`}
              value={scopeText}
              onChange={(e) => setScopeText(e.target.value)}
              className="h-16 font-mono text-[11px]"
            />
          </div>
          <div className="flex flex-col gap-2">
            <Select value={action} onValueChange={(v) => setAction(v as FilterAction)}>
              <SelectTrigger className="h-8 w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="ignore">ignore</SelectItem>
                <SelectItem value="keep">keep</SelectItem>
                <SelectItem value="archive">archive</SelectItem>
              </SelectContent>
            </Select>
            <Input
              type="number"
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
              className="h-8 w-32"
              placeholder="prioridad"
            />
          </div>
          <Button size="sm" disabled={busy || !scopeText.trim()} onClick={create} className="self-start">
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
            Crear
          </Button>
        </div>

        {/* Lista */}
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !rules ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Cargando reglas…
          </div>
        ) : !rules || rules.length === 0 ? (
          <EmptyState title="Sin reglas" hint="Creá una arriba o bloqueá un remitente desde un mensaje." />
        ) : (
          <div className="divide-y divide-border rounded-md border border-border">
            {rules.map((r) => (
              <div key={r.id} className="flex flex-wrap items-center gap-2 px-3 py-2">
                <StatusBadge tone={r.enabled ? "ok" : "neutral"} label={r.enabled ? "ON" : "OFF"} />
                <span className="num text-xs font-medium">{r.action}</span>
                <span className="num text-[11px] text-muted-foreground">
                  prio {r.priority} · {r.sourceType ?? "*"}
                  {r.sourceId != null ? `/${r.sourceId}` : ""}
                </span>
                <span className="num min-w-0 flex-1 truncate text-[11px] text-muted-foreground" title={JSON.stringify(r.scope)}>
                  {JSON.stringify(r.scope)}
                </span>
                <Button
                  variant="outline"
                  size="xs"
                  disabled={busy}
                  onClick={() => mutate(async () => void (await updateFilter(r.id, { enabled: !r.enabled })), r.enabled ? "Desactivada" : "Activada")}
                >
                  {r.enabled ? "Desactivar" : "Activar"}
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  disabled={busy}
                  onClick={() => mutate(async () => deleteFilter(r.id), "Regla borrada")}
                  title="Borrar"
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
