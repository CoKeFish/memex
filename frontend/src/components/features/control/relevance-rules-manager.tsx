// Reglas automáticas del gate de relevancia + cola de revisión manual. Las reglas son COMPUESTAS
// (un remitente y/o un patrón del asunto) y BIPOLARES (`block` → no-relevante; `allow` → relevante,
// entra sin juez). Las propone la minería LLM (o el dueño a mano) y SOLO se activan si su dry run
// contra el histórico no atrapa ningún correo del lado contrario; el reporte queda persistido
// (auditoría) y el toggle es reversible. La cola junta los `insufficient` (el gate no adivina).

import { useState } from "react"
import { Link } from "react-router-dom"
import {
  Ban,
  Check,
  ChevronDown,
  ChevronRight,
  Loader2,
  Pickaxe,
  ShieldAlert,
  Star,
  X,
} from "lucide-react"
import { toast } from "sonner"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import {
  confirmGateRule,
  createGateRule,
  fetchGateRules,
  fetchReviewQueue,
  fetchRuleAudits,
  mineGateRules,
  patchGateRule,
  rejudgeGateRule,
  resolveReview,
  runRuleAudit,
} from "@/data"
import type { GateRule, MatchField, ReviewItem, RuleAudit, RuleEffect } from "@/data"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import { cn } from "@/lib/utils"

const SENDER_KIND_LABEL: Record<NonNullable<GateRule["senderKind"]>, string> = {
  sender_email: "remitente",
  sender_domain: "dominio",
  list_id: "list-id",
}

function errMsg(e: unknown): string {
  return e instanceof ApiError ? String(e.detail) : e instanceof Error ? e.message : String(e)
}

/** Texto de los predicados de una regla compuesta (remitente y/o regex sobre un campo). */
function rulePredicates(rule: GateRule): string {
  const parts: string[] = []
  if (rule.senderKind) parts.push(`${SENDER_KIND_LABEL[rule.senderKind]}=${rule.senderValue}`)
  if (rule.pattern) parts.push(`${rule.matchField}~"${rule.pattern}"`)
  return parts.join(" & ") || "(sin predicados)"
}

function effectBadge(effect: RuleEffect) {
  return effect === "allow" ? (
    <Badge className="border-status-ok/40 bg-status-ok/10 text-status-ok">permitir</Badge>
  ) : (
    <Badge className="border-status-error/40 bg-status-error/10 text-status-error">bloquear</Badge>
  )
}

function statusBadge(status: GateRule["status"]) {
  if (status === "active") return <Badge variant="secondary">activa</Badge>
  if (status === "disabled") return <Badge variant="outline">apagada</Badge>
  return <Badge variant="outline">rechazada</Badge>
}

/** Segundas opiniones del juez sobre los correos que la regla decidió sola (se cargan al abrir). */
function RuleAudits({ ruleId }: { ruleId: number }) {
  const audits = useAsync<RuleAudit[]>(() => fetchRuleAudits(ruleId), [ruleId])
  if (audits.error) return <ErrorState detail={audits.error} onRetry={audits.reload} />
  if (!audits.data) {
    return (
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <Loader2 className="size-3 animate-spin" /> Cargando auditorías…
      </div>
    )
  }
  if (audits.data.length === 0) {
    return (
      <div className="text-[11px] text-muted-foreground">
        Sin auditorías todavía: el juez opina sobre 1 de cada N correos que esta regla decide (el
        primero siempre), o cuando corrés «Auditar reglas».
      </div>
    )
  }
  return (
    <ul className="space-y-1 text-[11px]">
      {audits.data.map((a) => (
        <li key={a.id} className="flex flex-wrap items-baseline gap-x-2">
          <span
            className={cn(
              "num shrink-0 font-medium",
              a.agrees === false
                ? "text-status-error"
                : a.agrees === true
                  ? "text-status-ok"
                  : "text-muted-foreground",
            )}
          >
            {a.agrees === false ? "contradice" : a.agrees === true ? "coincide" : "insuficiente"}
          </span>
          <Link to={`/datos/${a.inboxId}`} className="num text-muted-foreground hover:underline">
            #{a.inboxId}
          </Link>
          <span className="min-w-0 truncate" title={a.subject ?? ""}>
            {a.sender} · {a.subject}
          </span>
          <span className="min-w-0 flex-1 truncate text-muted-foreground" title={a.judgeReason}>
            juez: {a.judgeVerdict} — {a.judgeReason}
          </span>
        </li>
      ))}
    </ul>
  )
}

function RuleRow({
  rule,
  busy,
  onToggle,
  onConfirm,
  onRejudge,
}: {
  rule: GateRule
  busy: boolean
  onToggle: (id: number, status: "active" | "disabled") => void
  onConfirm: (id: number) => void
  onRejudge: (id: number) => void
}) {
  // Una regla que pide revisión arranca abierta: la decisión (confirmar / apagar y re-juzgar) está
  // a un clic, con los correos que la contradicen a la vista.
  // Estado derivado sin efecto: si el dueño no tocó el chevrón, manda `needsReview`.
  const [manualOpen, setManualOpen] = useState<boolean | null>(null)
  const open = manualOpen ?? rule.needsReview
  const setOpen = (v: boolean) => setManualOpen(v)
  const report = rule.dryRunReport
  const lado = rule.effect === "allow" ? "no-relevantes" : "relevantes"
  return (
    <div className={cn("px-3 py-2", rule.needsReview && "bg-status-review/5")}>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="text-muted-foreground"
          onClick={() => setOpen(!open)}
          title="Ver dry run y auditorías"
        >
          {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        </button>
        {effectBadge(rule.effect)}
        {statusBadge(rule.status)}
        {rule.needsReview && (
          <Badge
            className="gap-1 border-status-review/40 bg-status-review/10 text-status-review"
            title="el juez contradijo esta regla; revisala: confirmar o apagar y re-juzgar"
          >
            <ShieldAlert className="size-3" /> para revisar
          </Badge>
        )}
        <span className="num min-w-0 text-xs font-medium">{rulePredicates(rule)}</span>
        <span className="text-[11px] text-muted-foreground">
          {rule.proposedBy === "llm" ? "minería" : "manual"}
        </span>
        {rule.status === "active" && (
          <span
            className="num text-[11px] text-muted-foreground"
            title="decisiones desde que existe el contador · auditadas por el juez · contradicciones desde la última revisión"
          >
            {rule.hitCount} decid. · {rule.auditCount} audit. ·{" "}
            <span className={rule.contradictions > 0 ? "text-status-review" : undefined}>
              {rule.contradictions} contrad.
            </span>
          </span>
        )}
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
      {open && (
        <div className="mt-2 space-y-2 rounded-md border border-border bg-muted/20 p-2">
          {report ? (
            <div className="text-[11px] text-muted-foreground">
              dry run: matcheó {report.matched} correo(s) — {report.matchedRelevant} relevante(s),{" "}
              {report.matchedNotRelevant} no relevante(s), {report.matchedUnverdicted} sin veredicto.{" "}
              {report.passes ? `Pasó (no atrapa ${lado}).` : `NO pasó: atraparía ${lado}`}
              {report.contaminatingSampleIds.length > 0 && (
                <> (ej. inbox {report.contaminatingSampleIds.slice(0, 5).join(", ")})</>
              )}
            </div>
          ) : (
            <div className="text-[11px] text-muted-foreground">(sin reporte de dry run)</div>
          )}
          <RuleAudits ruleId={rule.id} />
          {rule.status === "active" && (
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <Button
                size="sm"
                variant="outline"
                className="h-7 text-xs"
                disabled={busy}
                onClick={() => onConfirm(rule.id)}
                title="la regla sigue tal cual; el contador de contradicciones vuelve a cero"
              >
                <Check className="size-3.5" /> Confirmar regla
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-7 text-xs"
                disabled={busy}
                onClick={() => onRejudge(rule.id)}
                title="apaga la regla y vuelve a pasar por el gate los correos que decidió"
              >
                <Ban className="size-3.5" /> Apagar y re-juzgar
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function RelevanceRulesManager() {
  const rules = useAsync<GateRule[]>(() => fetchGateRules("all"), [])
  const review = useAsync<ReviewItem[]>(() => fetchReviewQueue(), [])
  const [busy, setBusy] = useState(false)
  const [senderValue, setSenderValue] = useState("")
  const [pattern, setPattern] = useState("")
  const [matchField, setMatchField] = useState<MatchField>("subject")

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

  /** Alta manual de una regla compuesta: remitente (email→sender_email, si no →sender_domain) y/o
   *  patrón de asunto. `block` la marca no-relevante; `allow` la deja entrar sin juez. El dry run la
   *  rechaza si atraparía un correo del lado contrario. */
  const submit = (effect: RuleEffect) =>
    void mutate(
      async () => {
        const sv = senderValue.trim().toLowerCase()
        const pat = pattern.trim()
        await createGateRule({
          effect,
          senderKind: sv ? (sv.includes("@") ? "sender_email" : "sender_domain") : null,
          senderValue: sv || null,
          pattern: pat || null,
          matchField: pat ? matchField : null,
          rationale: "manual desde /filtros",
        })
        setSenderValue("")
        setPattern("")
      },
      effect === "block" ? "Regla de bloqueo creada" : "Regla de interés creada",
      rules.reload,
    )

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

  const confirm = (id: number) =>
    void mutate(
      async () => {
        await confirmGateRule(id)
      },
      "Regla confirmada: contradicciones a cero",
      rules.reload,
    )

  const rejudge = (id: number) =>
    void mutate(
      async () => {
        const r = await rejudgeGateRule(id, true)
        const g = r.gate
        toast.info(
          `Re-juzgados ${r.targets}: ${g.relevant ?? 0} relevantes, ${g.not_relevant ?? 0} no relevantes` +
            (g.by_rule ? `, ${g.by_rule} aún por otra regla` : ""),
        )
      },
      "Regla apagada y correos re-juzgados",
      rules.reload,
    )

  const audit = () =>
    void mutate(
      async () => {
        const r = await runRuleAudit(3)
        toast.info(
          `Auditoría: ${r.audited} correos de ${r.rules} reglas — ${r.contradictions} contradicciones, ${r.agreements} coincidencias` +
            (r.costUsd ? ` · $${r.costUsd.toFixed(4)}` : ""),
        )
      },
      "Auditoría corrida",
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

  const noPredicate = !senderValue.trim() && !pattern.trim()

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="filtros · gate de relevancia"
        title="Reglas automáticas y revisión"
        sub="reglas compuestas (remitente y/o patrón regex sobre asunto/cuerpo) y bipolares (bloquear / permitir), validadas con dry run contra el histórico (atrapar un correo del lado contrario = rechazada); la cola junta los correos donde el gate no pudo decidir"
        right={
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={audit}
              title="el juez da su segunda opinión sobre hasta 3 correos ya decididos por cada regla activa"
            >
              {busy ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <ShieldAlert className="size-3.5" />
              )}
              Auditar reglas (juez)
            </Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={mine}>
              {busy ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Pickaxe className="size-3.5" />
              )}
              Minar reglas (LLM)
            </Button>
          </div>
        }
      />
      <PanelBody className="space-y-4">
        {/* Alta manual de regla compuesta: remitente y/o asunto, bloquear o permitir */}
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            <Input
              placeholder="remitente (email o dominio)"
              value={senderValue}
              onChange={(e) => setSenderValue(e.target.value)}
              className="h-8 min-w-[160px] flex-1"
            />
            <Input
              placeholder="patrón regex (minúscula, opcional)"
              value={pattern}
              onChange={(e) => setPattern(e.target.value)}
              className="h-8 min-w-[160px] flex-1"
            />
            <Select value={matchField} onValueChange={(v) => setMatchField(v as MatchField)}>
              <SelectTrigger className="h-8 w-auto min-w-[110px] text-xs" aria-label="Campo">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="subject" className="text-xs">
                  asunto
                </SelectItem>
                <SelectItem value="body" className="text-xs">
                  cuerpo
                </SelectItem>
                <SelectItem value="subject_or_body" className="text-xs">
                  asunto o cuerpo
                </SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={busy || noPredicate}
              onClick={() => submit("block")}
            >
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Ban className="size-3.5" />}
              Bloquear
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy || noPredicate}
              onClick={() => submit("allow")}
            >
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Star className="size-3.5" />}
              Marcar como de interés
            </Button>
            <span className="text-[11px] text-muted-foreground">
              remitente y/o patrón regex — el dry run valida contra el histórico
            </span>
          </div>
        </div>

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
            hint="La minería (botón arriba) propone reglas a partir de lo que el gate marcó relevante o no-relevante; también podés crearlas arriba o por CLI (memex-relevance rules add)."
          />
        ) : (
          <div className="divide-y divide-border rounded-md border border-border">
            {/* Las que piden revisión primero: son la acción pendiente del dueño. */}
            {[...rules.data]
              .sort((a, b) => Number(b.needsReview) - Number(a.needsReview))
              .map((r) => (
                <RuleRow
                  key={r.id}
                  rule={r}
                  busy={busy}
                  onToggle={toggle}
                  onConfirm={confirm}
                  onRejudge={rejudge}
                />
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
