// Actividad / logs del módulo finance. Reconstruye los "logs" del módulo desde la tabla `llm_calls`
// (cada corrida de extracción se persiste ahí, igual que la traza de un mensaje): qué procesó, cuántos
// gastos extrajo/descartó, errores y costo. Reusa la API real `/metrics/llm/calls?module=finance`.
import { Link } from "react-router-dom"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { KpiCard } from "@/components/common/kpi-card"
import { StatusBadge } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { Skeleton } from "@/components/ui/skeleton"
import { RecentErrors } from "@/components/features/metrics/recent-errors"
import { formatCompact, formatInt, formatUsd } from "@/lib/format"
import { llmTone } from "@/lib/status"
import { fetchLlmCalls } from "@/data"
import { useAsync } from "@/lib/use-async"
import type { LlmStatus } from "@/types/domain"

// El endpoint topea en 200 filas; finance rara vez supera eso, pero lo señalamos si pasa.
const MAX = 200
const STATUS_LABEL: Record<string, string> = { ok: "OK", error: "Error", filtered: "Filtrado" }

/** Lee un entero de la metadata de la llamada (p. ej. items/discarded de la extracción). */
function metaInt(meta: Record<string, unknown> | null, key: string): number {
  const v = meta?.[key]
  return typeof v === "number" ? v : 0
}

export function FinanceActivity() {
  const { data, loading, error, reload } = useAsync(
    () => fetchLlmCalls({ module: ["finance"], sort: "created_at", dir: "desc", limit: MAX }),
    [],
  )
  const rows = data?.items ?? []
  const total = data?.total ?? 0
  const capped = total > rows.length
  const errors = rows.filter((r) => r.status === "error").length
  const items = rows.reduce((a, r) => a + metaInt(r.metadata, "items"), 0)
  const discarded = rows.reduce((a, r) => a + metaInt(r.metadata, "discarded"), 0)
  const lastRun = rows[0]?.createdAt ?? null

  return (
    <section className="space-y-4">
      <div>
        <div className="eyebrow">módulo finance · actividad</div>
        <h2 className="text-lg font-semibold tracking-tight">Logs de extracción</h2>
        <p className="text-xs text-muted-foreground">
          Cada corrida del módulo (tabla llm_calls): qué procesó, cuántos gastos sacó/descartó y errores.
          {capped && ` · mostrando las últimas ${rows.length} de ${total}`}
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <KpiCard
          eyebrow="Última corrida"
          value={lastRun ? <RelativeTime date={lastRun} /> : "—"}
          footer={lastRun ? "última llamada del módulo" : "el módulo nunca corrió"}
        />
        <KpiCard eyebrow="Llamadas" value={formatInt(total)} footer="al LLM (extracción)" />
        <KpiCard
          eyebrow="Gastos extraídos"
          value={formatInt(items)}
          footer={`${formatInt(discarded)} descartados`}
        />
        <KpiCard
          eyebrow="Errores"
          value={formatInt(errors)}
          footer={capped ? "en las últimas corridas" : "en todas las corridas"}
        />
      </div>

      <div className="grid gap-5 xl:grid-cols-[1fr_1.4fr]">
        <RecentErrors window={{}} module="finance" />
        <Panel>
          <PanelHeader eyebrow="Auditoría · llm_calls" title="Corridas recientes" sub="Más nuevas primero · saltá a la traza del mensaje" />
          <PanelBody className="p-0">
            {error ? (
              <ErrorState detail={error} onRetry={reload} />
            ) : loading && !data ? (
              <div className="space-y-2 p-4">
                {Array.from({ length: 5 }).map((_, i) => (
                  <Skeleton key={i} className="h-6 w-full" />
                ))}
              </div>
            ) : rows.length === 0 ? (
              <EmptyState title="Sin corridas" hint="El módulo finance todavía no procesó nada." />
            ) : (
              <div className="max-h-[460px] overflow-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border bg-muted/30 text-left">
                      <th className="px-3 py-2 font-medium text-muted-foreground">Hora</th>
                      <th className="px-3 py-2 font-medium text-muted-foreground">Estado</th>
                      <th className="px-3 py-2 text-right font-medium text-muted-foreground">Gastos (ext/desc)</th>
                      <th className="px-3 py-2 text-right font-medium text-muted-foreground">Tokens (p/c)</th>
                      <th className="px-3 py-2 text-right font-medium text-muted-foreground">Costo</th>
                      <th className="px-3 py-2 font-medium text-muted-foreground">inbox</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {rows.map((c) => (
                      <tr key={c.id} className="align-top hover:bg-accent/30">
                        <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                          <RelativeTime date={c.createdAt} />
                        </td>
                        <td className="px-3 py-2">
                          <span title={c.errorMessage ?? undefined}>
                            <StatusBadge tone={llmTone(c.status as LlmStatus)} label={STATUS_LABEL[c.status] ?? c.status} />
                          </span>
                        </td>
                        <td className="num px-3 py-2 text-right text-muted-foreground">
                          {c.status === "ok" ? (
                            <>
                              {metaInt(c.metadata, "items")}
                              <span className="opacity-50"> / </span>
                              {metaInt(c.metadata, "discarded")}
                            </>
                          ) : (
                            <span className="opacity-50">—</span>
                          )}
                        </td>
                        <td className="num px-3 py-2 text-right text-muted-foreground">
                          {formatCompact(c.promptTokens)}
                          <span className="opacity-50"> / </span>
                          {formatCompact(c.completionTokens)}
                        </td>
                        <td className="num px-3 py-2 text-right font-medium">
                          {c.status === "ok" ? formatUsd(c.costUsd) : <span className="text-muted-foreground">—</span>}
                        </td>
                        <td className="num px-3 py-2 text-muted-foreground">
                          {c.inboxId !== null ? (
                            <Link to={`/datos/${c.inboxId}`} className="text-origin-inbox hover:underline">
                              #{c.inboxId}
                            </Link>
                          ) : (
                            <span className="opacity-50" title="batch: la llamada cubre N mensajes">batch</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </PanelBody>
        </Panel>
      </div>
    </section>
  )
}
