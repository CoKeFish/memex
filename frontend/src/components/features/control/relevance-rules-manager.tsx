// Reglas automáticas del gate de relevancia + cola de revisión manual. Las reglas las propone
// la minería LLM sobre los no-relevantes (o el dueño a mano) y SOLO se activan si su dry run
// contra el histórico no atrapa ningún correo relevante; el reporte queda persistido
// (auditoría) y el toggle es reversible. La cola junta los `insufficient` (el gate no adivina).

import { useState } from "react"
import { Check, ChevronDown, ChevronRight, Loader2, Pickaxe, X } from "lucide-react"
import { toast } from "sonner"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { fetchGateRules, fetchReviewQueue, mineGateRules, patchGateRule, resolveReview } from "@/data"
import type { GateRule, ReviewItem } from "@/data"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"

const KIND_LABEL: Record<GateRule["kind"], string> = {
  sender_email: "remitente",
  sender_domain: "dominio",
  subject_contains: "asunto contiene",
  list_id: "list-id",
}

function errMsg(e: unknown): string {
  return e instanceof ApiError ? String(e.detail) : e instanceof Error ? e.message : String(e)
}

function statusBadge(status: GateRule["status"]) {
  if (status === "active") return <Badge className="bg-status-ok/15 text-status-ok">activa</Badge>
  if (status === "disabled") return <Badge variant="secondary">apagada</Badge>
  return <Badge className="bg-status-error/15 text-status-error">rechazada</Badge>
}

function RuleRow({
  rule,
  busy,
  onToggle,
}: {
  rule: GateRule
  busy: boolean
  onToggle: (id: number, status: "active" | "disabled") => void
}) {
  const [open, setOpen] = useState(false)
  const report = rule.dryRunReport
  return (
    <div className="px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="text-muted-foreground"
          onClick={() => setOpen(!open)}
          title="Ver reporte del dry run"
        >
          {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        </button>
        {statusBadge(rule.status)}
        <span className="text-xs text-muted-foreground">{KIND_LABEL[rule.kind]}</span>
        <span className="num min-w-0 text-xs font-medium">{rule.pattern}</span>
        <span className="text-[11px] text-muted-foreground">
          {rule.proposedBy === "llm" ? "minería" : "manual"}
        </span>
        {rule.rationale && (
          <span
            className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground"
            title={rule.rationale}
          >
            {rule.rationale}
          </span>
        )}
        {rule.status !== "rejected" && (
          <Switch
            className="ml-auto"
            checked={rule.status === "active"}
            disabled={busy}
            onCheckedChange={(v) => onToggle(rule.id, v ? "active" : "disabled")}
          />
        )}
      </div>
      {open && report && (
        <div className="mt-2 rounded-md border border-border bg-muted/20 p-2 text-[11px] text-muted-foreground">
          dry run: matcheó {report.matched} correo(s) — {report.matchedRelevant} relevante(s),{" "}
          {report.matchedNotRelevant} no relevante(s), {report.matchedUnverdicted} sin veredicto.{" "}
          {report.passes ? "Pasó (no atrapa relevantes)." : "NO pasó: atraparía relevantes"}
          {report.relevantSampleIds.length > 0 && (
            <> (ej. inbox {report.relevantSampleIds.slice(0, 5).join(", ")})</>
          )}
        </div>
      )}
      {open && !report && (
        <div className="mt-2 text-[11px] text-muted-foreground">(sin reporte de dry run)</div>
      )}
    </div>
  )
}

export function RelevanceRulesManager() {
  const rules = useAsync<GateRule[]>(() => fetchGateRules("all"), [])
  const review = useAsync<ReviewItem[]>(() => fetchReviewQueue(), [])
  const [busy, setBusy] = useState(false)

  async function mutate(fn: () => Promise<void>, ok: string, reload: () => void) {
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

  const mine = () =>
    void mutate(
      async () => {
        const r = await mineGateRules()
        toast.info(
          `Minería: ${r.proposed} propuestas — ${r.activated} activadas, ${r.rejected} rechazadas, ${r.skipped} duplicadas`,
        )
      },
      "Minería corrida",
      rules.reload,
    )

  const toggle = (id: number, status: "active" | "disabled") =>
    void mutate(
      async () => {
        await patchGateRule(id, status)
      },
      status === "active" ? "Regla activada" : "Regla desactivada",
      rules.reload,
    )

  const resolve = (inboxId: number, isRelevant: boolean) =>
    void mutate(
      async () => {
        await resolveReview(inboxId, isRelevant)
      },
      isRelevant ? "Marcado relevante" : "Marcado no relevante",
      review.reload,
    )

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="filtros · gate de relevancia"
        title="Reglas automáticas y revisión"
        sub="reglas deterministas validadas con dry run contra el histórico (atrapar 1 relevante = rechazada); la cola junta los correos donde el gate no pudo decidir"
        right={
          <Button size="sm" variant="outline" disabled={busy} onClick={mine}>
            {busy ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Pickaxe className="size-3.5" />
            )}
            Minar reglas (LLM)
          </Button>
        }
      />
      <PanelBody className="space-y-4">
        {/* Reglas */}
        {rules.error ? (
          <ErrorState detail={rules.error} onRetry={rules.reload} />
        ) : !rules.data ? (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Cargando reglas…
          </div>
        ) : rules.data.length === 0 ? (
          <EmptyState
            title="Sin reglas"
            hint="La minería (botón arriba) propone reglas a partir de lo que el gate marcó no relevante; también podés crearlas por CLI (memex-relevance rules add)."
          />
        ) : (
          <div className="divide-y divide-border rounded-md border border-border">
            {rules.data.map((r) => (
              <RuleRow key={r.id} rule={r} busy={busy} onToggle={toggle} />
            ))}
          </div>
        )}

        {/* Cola de revisión manual */}
        <div className="space-y-2">
          <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Cola de revisión ({review.data?.length ?? 0})
          </div>
          {review.error ? (
            <ErrorState detail={review.error} onRetry={review.reload} />
          ) : !review.data ? (
            <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" /> Cargando cola…
            </div>
          ) : review.data.length === 0 ? (
            <EmptyState title="Sin pendientes" hint="El gate no tiene correos en duda." />
          ) : (
            <div className="divide-y divide-border rounded-md border border-border">
              {review.data.map((it) => (
                <div key={it.inboxId} className="space-y-1 px-3 py-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <a
                      href={`/datos/${it.inboxId}`}
                      className="num text-xs font-medium underline-offset-2 hover:underline"
                    >
                      #{it.inboxId}
                    </a>
                    <span className="text-xs text-muted-foreground">{it.fromEmail ?? "—"}</span>
                    <span className="min-w-0 flex-1 truncate text-xs font-medium">
                      {it.subject ?? "(sin asunto)"}
                    </span>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() => resolve(it.inboxId, true)}
                    >
                      <Check className="size-3.5 text-status-ok" /> Relevante
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() => resolve(it.inboxId, false)}
                    >
                      <X className="size-3.5 text-status-error" /> No relevante
                    </Button>
                  </div>
                  {it.reason && (
                    <div className="text-[11px] text-muted-foreground">duda: {it.reason}</div>
                  )}
                  {it.snippet && (
                    <div className="truncate text-[11px] text-muted-foreground">{it.snippet}</div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </PanelBody>
    </Panel>
  )
}
