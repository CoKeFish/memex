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
  clearSenderTier,
  createFilter,
  fetchCandidates,
  fetchSenderRelevance,
  judgeSender,
  setCandidateStatus,
  setSenderTier,
} from "@/data"
import type { RelevanceCandidate, SenderRelevance } from "@/data"
import { ApiError } from "@/lib/api"
import { cn } from "@/lib/utils"
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

type PendingAction = { email: string; kind: "no_procesar" | "descartar"; candidateKey?: string }

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
  const visible = kindFilter === "all" ? rows : rows.filter((r) => r.kind === kindFilter)

  async function runAction(fn: () => Promise<unknown>, msg: string) {
    setBusy(true)
    try {
      await fn()
      toast.success(msg)
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
    const msg = p.kind === "no_procesar" ? `No se procesarán: ${p.email}` : `Descartado: ${p.email}`
    void runAction(async () => {
      if (p.kind === "no_procesar") await setSenderTier(p.email)
      else await createFilter({ scope: { "from.email": { equals: p.email } }, action: "ignore" })
      if (p.candidateKey) await setCandidateStatus(p.candidateKey, "confirmed")
    }, msg)
  }

  function dismissCandidate(key: string, label: string) {
    void runAction(() => setCandidateStatus(key, "dismissed"), `Sacado de la cola: ${label}`)
  }

  function judge(key: string) {
    void runAction(() => judgeSender(key), "Juez LLM consultado")
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="categoría · calidad"
        title="Relevancia por remitente"
        description="Qué tan seguido cada remitente produjo un hecho de dominio (relevante) frente a solo leerse o quedar inerte (ruido). Determinista, sin LLM, con el ruido primero. Desde acá podés mandar un remitente a 'no procesar' (se guarda, sin gasto LLM) o descartarlo (drop puro). El % cuenta solo los mensajes con un hecho extraído; 'solo lectura' e 'inertes' van aparte."
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
            Candidatos detectados (auto) <Badge variant="secondary">{candidates.length}</Badge>
          </div>
          <p className="mb-2 text-xs text-muted-foreground">
            Remitentes que el sistema marcó como ruidosos (volumen alto, poca relevancia). Confirmá
            la acción o sacalos de la cola.
          </p>
          <div className="space-y-1.5">
            {candidates.map((c) => {
              const cemail = c.email
              return (
                <div
                  key={c.senderKey}
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border bg-card/40 px-3 py-2 text-sm"
                >
                  <div className="min-w-0 flex-1">
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
                    {c.llmVerdict && (
                      <span
                        className={`ml-2 text-xs ${c.llmVerdict.isRelevant ? "text-emerald-600 dark:text-emerald-400" : "text-amber-600 dark:text-amber-400"}`}
                      >
                        · LLM: {c.llmVerdict.isRelevant ? "relevante" : "ruido"}
                        {c.llmVerdict.reason ? ` — ${c.llmVerdict.reason}` : ""}
                      </span>
                    )}
                  </div>
                  {cemail && (
                    <div className="flex gap-1">
                      <Button
                        size="xs"
                        variant="outline"
                        disabled={busy}
                        onClick={() =>
                          setPending({ email: cemail, kind: "no_procesar", candidateKey: c.senderKey })
                        }
                      >
                        No procesar
                      </Button>
                      <Button
                        size="xs"
                        variant="ghost"
                        disabled={busy}
                        onClick={() =>
                          setPending({ email: cemail, kind: "descartar", candidateKey: c.senderKey })
                        }
                      >
                        Descartar
                      </Button>
                    </div>
                  )}
                  <Button
                    size="xs"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => judge(c.senderKey)}
                  >
                    Juzgar (LLM)
                  </Button>
                  <Button
                    size="xs"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => dismissCandidate(c.senderKey, c.senderLabel)}
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
                <th className="px-3 py-2 text-right font-medium">Mensajes</th>
                <th className="px-3 py-2 text-right font-medium">% relevancia</th>
                <th className="px-3 py-2 text-right font-medium">Relevantes</th>
                <th className="px-3 py-2 text-right font-medium">Solo lectura</th>
                <th className="px-3 py-2 text-right font-medium">Inertes</th>
                <th className="px-3 py-2 text-right font-medium">Marcados</th>
                <th className="px-3 py-2 text-right font-medium">Volumen</th>
                <th className="px-3 py-2 font-medium">Último</th>
                <th className="px-3 py-2 font-medium">Tiers</th>
                <th className="px-3 py-2 font-medium">Acciones</th>
              </tr>
            </thead>
            <tbody>
              {visible.length === 0 && (
                <tr>
                  <td colSpan={11} className="px-3 py-6 text-center text-sm text-muted-foreground">
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
                      ) : r.overrideTier ? (
                        <div className="flex items-center gap-1.5">
                          <Badge variant="secondary" title="No se procesa (tier forzado)">
                            no procesar
                          </Badge>
                          <Button
                            size="xs"
                            variant="ghost"
                            disabled={busy}
                            onClick={() => void runAction(() => clearSenderTier(email), `Reactivado: ${email}`)}
                          >
                            Reactivar
                          </Button>
                        </div>
                      ) : (
                        <div className="flex gap-1">
                          <Button
                            size="xs"
                            variant="outline"
                            disabled={busy}
                            onClick={() => setPending({ email, kind: "no_procesar" })}
                          >
                            No procesar
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
              {pending?.kind === "descartar" ? "Descartar remitente" : "No procesar remitente"}
            </DialogTitle>
            <DialogDescription>
              {pending?.kind === "descartar"
                ? `Los próximos mensajes de ${pending?.email} se filtrarán antes de guardarse (drop puro: se olvidan). Reversible en Filtros.`
                : `Los próximos mensajes de ${pending?.email} se guardarán pero no se procesarán (tier blacklist: sin gasto LLM). No se borran; reversible acá mismo.`}
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
