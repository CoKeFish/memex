import { useState } from "react"
import { Loader2 } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { PageHeader } from "@/components/common/page-header"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  createFilter,
  createGateRule,
  fetchCandidates,
  fetchSenderRelevance,
  reevaluateCandidate,
  setCandidateStatus,
} from "@/data"
import type { RelevanceCandidate, SenderRelevance } from "@/data"
import { ApiError } from "@/lib/api"
import { cn } from "@/lib/utils"
import { formatUsd } from "@/lib/format"
import { useAsync } from "@/lib/use-async"

/** Fecha corta `YYYY-MM-DD` desde un ISO. */
function shortDate(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "—"
}

/** Cue de color del % de relevancia: bajo = ruido (rojo), medio (ámbar), alto = útil (verde). */
function pctClass(pct: number | null): string {
  if (pct === null) return "text-muted-foreground"
  if (pct < 20) return "text-red-600 dark:text-red-400"
  if (pct < 60) return "text-amber-600 dark:text-amber-400"
  return "text-emerald-600 dark:text-emerald-400"
}

/** Resumen legible de la mezcla de tiers. */
function tierMixLabel(mix: Record<string, number>): string {
  const order: [string, string][] = [
    ["blacklist", "blacklist"],
    ["batch", "batch"],
    ["individual", "individual"],
    ["unclassified", "sin clasificar"],
  ]
  const parts = order.filter(([k]) => mix[k]).map(([k, label]) => `${mix[k]} ${label}`)
  return parts.join(" · ") || "—"
}

/** Etiqueta legible del procedimiento que detectó al candidato. */
const PROC_LABELS: Record<string, string> = {
  fact_count: "procesado sin hecho",
  sender_relevance: "remitente ruidoso",
}
function procLabel(p: string): string {
  return PROC_LABELS[p] ?? p
}

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

type KindFilter = "all" | "email" | "chat" | "social"
const KIND_FILTERS: { value: KindFilter; label: string }[] = [
  { value: "all", label: "Todos" },
  { value: "email", label: "Correo" },
  { value: "chat", label: "Chat" },
  { value: "social", label: "Redes" },
]

type PendingAction = {
  email: string
  kind: "bloquear" | "descartar"
  candidateKey?: string
  procedure?: string
}

/** Columnas numéricas ordenables (`costPerMsg` = costUsd/messages, derivada en el front). */
type SortKey =
  | "messages"
  | "relevancePct"
  | "relevant"
  | "summarizedOnly"
  | "inert"
  | "marked"
  | "volumeRatio"
  | "costUsd"
  | "costPerMsg"
type SortState = { key: SortKey; dir: "asc" | "desc" } | null

function sortValue(r: SenderRelevance, key: SortKey): number | null {
  switch (key) {
    case "messages":
      return r.messages
    case "relevant":
      return r.relevant
    case "summarizedOnly":
      return r.summarizedOnly
    case "inert":
      return r.inert
    case "marked":
      return r.marked
    case "relevancePct":
      return r.relevancePct
    case "volumeRatio":
      return r.volumeRatio
    case "costUsd":
      return r.costUsd
    case "costPerMsg":
      return r.costUsd === null ? null : r.costUsd / r.messages
  }
}

/** Ordena por la columna activa; los `null` van siempre al final. `sort` null = orden del server. */
function sortRows(rows: SenderRelevance[], sort: SortState): SenderRelevance[] {
  if (!sort) return rows
  const mult = sort.dir === "asc" ? 1 : -1
  return [...rows].sort((a, b) => {
    const av = sortValue(a, sort.key)
    const bv = sortValue(b, sort.key)
    if (av === null && bv === null) return 0
    if (av === null) return 1
    if (bv === null) return -1
    return (av - bv) * mult
  })
}

/** Encabezado numérico clickeable: alterna asc/desc y marca la dirección activa. */
function SortableTh({
  label,
  sortKey,
  sort,
  onSort,
}: {
  label: string
  sortKey: SortKey
  sort: SortState
  onSort: (k: SortKey) => void
}) {
  const active = sort?.key === sortKey
  return (
    <th className="px-3 py-2 text-right font-medium">
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        title="Ordenar por esta columna"
        className={cn(
          "inline-flex items-center gap-1 hover:text-foreground",
          active && "text-foreground",
        )}
      >
        {label}
        <span aria-hidden className={cn("text-[10px]", !active && "opacity-0")}>
          {sort?.dir === "asc" ? "▲" : "▼"}
        </span>
      </button>
    </th>
  )
}

export function SenderRelevancePage() {
  const { data, loading, error, reload } = useAsync<SenderRelevance[]>(
    () => fetchSenderRelevance(),
    [],
  )
  const { data: candidatesData, reload: reloadCandidates } = useAsync<RelevanceCandidate[]>(
    () => fetchCandidates("open"),
    [],
  )
  const rows = data ?? []
  const candidates = candidatesData ?? []
  const [pending, setPending] = useState<PendingAction | null>(null)
  const [busy, setBusy] = useState(false)
  const [kindFilter, setKindFilter] = useState<KindFilter>("all")
  const [procFilter, setProcFilter] = useState<string>("all")
  const [sort, setSort] = useState<SortState>(null)
  const filtered = kindFilter === "all" ? rows : rows.filter((r) => r.kind === kindFilter)
  const visible = sortRows(filtered, sort)
  /** Click en encabezado: la misma columna alterna dir; otra arranca en desc (más alto primero). */
  function toggleSort(key: SortKey) {
    setSort((cur) =>
      cur?.key === key ? { key, dir: cur.dir === "desc" ? "asc" : "desc" } : { key, dir: "desc" },
    )
  }
  const procedures = Array.from(new Set(candidates.map((c) => c.procedure)))
  const visibleCandidates =
    procFilter === "all" ? candidates : candidates.filter((c) => c.procedure === procFilter)

  /** Corre una mutación; si `fn` devuelve un string lo usa como detalle del toast de éxito. */
  async function runAction(fn: () => Promise<string | void>, msg: string) {
    setBusy(true)
    try {
      const detail = await fn()
      toast.success(msg, typeof detail === "string" ? { description: detail } : undefined)
      setPending(null)
      reload()
      reloadCandidates()
    } catch (e) {
      toast.error("No se pudo", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  function confirmPending() {
    if (!pending) return
    const p = pending
    const msg = p.kind === "bloquear" ? `Remitente bloqueado: ${p.email}` : `Descartado: ${p.email}`
    void runAction(async () => {
      if (p.kind === "bloquear")
        await createGateRule({
          effect: "block",
          senderKind: "sender_email",
          senderValue: p.email,
          rationale: "confirmado ruido desde /relevancia",
        })
      else await createFilter({ scope: { "from.email": { equals: p.email } }, action: "ignore" })
      if (p.candidateKey) await setCandidateStatus(p.candidateKey, "confirmed", p.procedure)
    }, msg)
  }

  function dismissCandidate(c: RelevanceCandidate) {
    void runAction(
      () => setCandidateStatus(c.senderKey, "dismissed", c.procedure),
      `Sacado de la cola: ${c.senderLabel}`,
    )
  }

  /** Re-evalúa la muestra del candidato por el MOTOR ÚNICO (el juez del gate + intereses). */
  function reevaluate(c: RelevanceCandidate) {
    void runAction(async () => {
      const r = await reevaluateCandidate(c.senderKey, c.procedure)
      return `${r.relevant} relevante(s), ${r.notRelevant} no relevante(s), ${r.insufficient} en duda (de ${r.messages})`
    }, "Re-evaluado por el motor único")
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="vista · relevancia"
        title="Relevancia por remitente"
        description="Capa de SEÑALES del sistema de relevancia: qué tan seguido cada remitente produjo un hecho de dominio (relevante) frente a solo leerse o quedar inerte (ruido). Determinista, sin LLM, ruido primero. Desde acá podés re-evaluar un candidato por el motor único (el juez del gate + tus intereses), bloquear un remitente (crea una regla del gate) o descartarlo (drop pre-ingest). El % cuenta solo los mensajes con un hecho extraído; 'solo lectura' e 'inertes' van aparte."
        actions={
          <div className="flex items-center gap-0.5 rounded-md border border-border p-0.5">
            {KIND_FILTERS.map((k) => (
              <button
                key={k.value}
                type="button"
                onClick={() => setKindFilter(k.value)}
                className={cn(
                  "rounded px-2.5 py-1 text-xs transition-colors",
                  kindFilter === k.value
                    ? "bg-accent text-foreground"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {k.label}
              </button>
            ))}
          </div>
        }
      />
      {candidates.length > 0 && (
        <div className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
          <div className="mb-1 flex items-center gap-2 text-sm font-medium">
            Candidatos a (re)evaluar <Badge variant="secondary">{candidates.length}</Badge>
          </div>
          <p className="mb-2 text-xs text-muted-foreground">
            Remitentes que un procedimiento determinista marcó para revisar (ej. procesado sin un
            hecho de dominio). Re-evaluá la muestra por el motor único, bloqueá el remitente (regla
            del gate) o sacalo de la cola.
          </p>
          {procedures.length > 1 && (
            <div className="mb-2 flex flex-wrap items-center gap-1">
              {["all", ...procedures].map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() => setProcFilter(p)}
                  className={cn(
                    "rounded border px-2 py-0.5 text-[11px] transition-colors",
                    procFilter === p
                      ? "border-amber-500/50 bg-amber-500/10 text-foreground"
                      : "border-border text-muted-foreground hover:text-foreground",
                  )}
                >
                  {p === "all" ? "Todos" : procLabel(p)}
                </button>
              ))}
            </div>
          )}
          <div className="space-y-1.5">
            {visibleCandidates.map((c) => {
              const cemail = c.email
              return (
                <div
                  key={`${c.procedure}:${c.senderKey}`}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border bg-card/40 px-3 py-2 text-sm"
                >
                  <div className="min-w-0 flex-1">
                    <Badge variant="outline" className="mr-2 align-middle text-[10px]">
                      {procLabel(c.procedure)}
                    </Badge>
                    <span className="font-medium">{c.senderLabel}</span>
                    <span className="ml-2 text-xs text-muted-foreground">
                      {c.messages} msj · {c.relevancePct === null ? "—" : `${c.relevancePct}%`}{" "}
                      relevancia · {c.inert} inertes
                    </span>
                    {c.sampleInboxIds.length > 0 && (
                      <span className="ml-2 text-xs">
                        {c.sampleInboxIds.map((id) => (
                          <Link
                            key={id}
                            to={`/datos/${id}`}
                            className="mr-1.5 underline underline-offset-2 hover:text-primary"
                          >
                            #{id}
                          </Link>
                        ))}
                      </span>
                    )}
                  </div>
                  <Button
                    size="xs"
                    variant="outline"
                    disabled={busy}
                    onClick={() => reevaluate(c)}
                    title="Corre el gate sobre la muestra (motor único)"
                  >
                    Re-evaluar
                  </Button>
                  {cemail && (
                    <Button
                      size="xs"
                      variant="ghost"
                      disabled={busy}
                      onClick={() =>
                        setPending({
                          email: cemail,
                          kind: "bloquear",
                          candidateKey: c.senderKey,
                          procedure: c.procedure,
                        })
                      }
                    >
                      Bloquear
                    </Button>
                  )}
                  <Button
                    size="xs"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => dismissCandidate(c)}
                  >
                    No es ruido
                  </Button>
                </div>
              )
            })}
          </div>
        </div>
      )}
      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading && !data ? (
        <div className="flex items-center justify-center gap-2 py-24 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Cargando relevancia…
        </div>
      ) : rows.length === 0 ? (
        <EmptyState
          title="Sin datos"
          hint="Todavía no hay mensajes clasificados/extraídos para medir relevancia. Procesá algunos y volvé."
        />
      ) : (
        <div className="overflow-x-auto rounded-lg border">
          <table className="w-full text-sm">
            <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-3 py-2 font-medium">Remitente</th>
                <SortableTh label="Mensajes" sortKey="messages" sort={sort} onSort={toggleSort} />
                <SortableTh
                  label="% relevancia"
                  sortKey="relevancePct"
                  sort={sort}
                  onSort={toggleSort}
                />
                <SortableTh label="Relevantes" sortKey="relevant" sort={sort} onSort={toggleSort} />
                <SortableTh
                  label="Solo lectura"
                  sortKey="summarizedOnly"
                  sort={sort}
                  onSort={toggleSort}
                />
                <SortableTh label="Inertes" sortKey="inert" sort={sort} onSort={toggleSort} />
                <SortableTh label="Marcados" sortKey="marked" sort={sort} onSort={toggleSort} />
                <SortableTh label="Volumen" sortKey="volumeRatio" sort={sort} onSort={toggleSort} />
                <SortableTh label="Costo LLM" sortKey="costUsd" sort={sort} onSort={toggleSort} />
                <SortableTh label="$/msg" sortKey="costPerMsg" sort={sort} onSort={toggleSort} />
                <th className="px-3 py-2 font-medium">Último</th>
                <th className="px-3 py-2 font-medium">Tiers</th>
                <th className="px-3 py-2 font-medium">Acciones</th>
              </tr>
            </thead>
            <tbody>
              {visible.length === 0 && (
                <tr>
                  <td colSpan={13} className="px-3 py-6 text-center text-sm text-muted-foreground">
                    Sin remitentes de este tipo.
                  </td>
                </tr>
              )}
              {visible.map((r) => {
                const email = r.email
                return (
                  <tr key={r.senderKey} className="border-t align-top">
                    <td className="px-3 py-2">
                      <div className="font-medium">{r.senderLabel}</div>
                      {r.senderLabel !== r.senderKey && (
                        <div className="text-xs text-muted-foreground">{r.senderKey}</div>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums">{r.messages}</td>
                    <td
                      className={`px-3 py-2 text-right font-medium tabular-nums ${pctClass(r.relevancePct)}`}
                    >
                      {r.relevancePct === null ? "—" : `${r.relevancePct}%`}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                      {r.relevant}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                      {r.summarizedOnly}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                      {r.inert}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                      {r.marked > 0 ? r.marked : "—"}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                      {r.volumeRatio === null ? "—" : `${r.volumeRatio}×`}
                    </td>
                    <td className="px-3 py-2 text-right font-medium tabular-nums">
                      {r.costUsd === null ? "—" : formatUsd(r.costUsd)}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                      {r.costUsd === null ? "—" : formatUsd(r.costUsd / r.messages)}
                    </td>
                    <td className="px-3 py-2 text-xs whitespace-nowrap text-muted-foreground">
                      {shortDate(r.lastAt)}
                    </td>
                    <td className="px-3 py-2 text-xs text-muted-foreground">
                      {tierMixLabel(r.tierMix)}
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      {email === null ? (
                        <span
                          className="text-xs text-muted-foreground"
                          title="Acción disponible para email; para chat, sacá el canal del allowlist."
                        >
                          —
                        </span>
                      ) : (
                        <div className="flex items-center gap-1.5">
                          {r.overrideTier && (
                            <Badge
                              variant="secondary"
                              title="tier forzado (dial de costo; se gestiona en Filtros)"
                            >
                              {r.overrideTier}
                            </Badge>
                          )}
                          <Button
                            size="xs"
                            variant="outline"
                            disabled={busy}
                            onClick={() => setPending({ email, kind: "bloquear" })}
                          >
                            Bloquear
                          </Button>
                          <Button
                            size="xs"
                            variant="ghost"
                            disabled={busy}
                            onClick={() => setPending({ email, kind: "descartar" })}
                          >
                            Descartar
                          </Button>
                        </div>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      <Dialog open={pending !== null} onOpenChange={(o) => !o && !busy && setPending(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {pending?.kind === "descartar" ? "Descartar remitente" : "Bloquear remitente"}
            </DialogTitle>
            <DialogDescription>
              {pending?.kind === "descartar"
                ? `Los próximos mensajes de ${pending?.email} se filtrarán antes de guardarse (drop puro: se olvidan). Reversible en Filtros.`
                : `Se creará una regla del gate (remitente = ${pending?.email}) que marca no-relevante sus próximos correos. Solo actúa con el gate encendido; reversible en Filtros → Reglas. Si su histórico tiene correos relevantes, la regla se rechaza.`}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" size="sm" disabled={busy} onClick={() => setPending(null)}>
              Cancelar
            </Button>
            <Button size="sm" disabled={busy} onClick={confirmPending}>
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : null}
              Confirmar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
