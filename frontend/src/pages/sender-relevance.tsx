import { Loader2 } from "lucide-react"
import { PageHeader } from "@/components/common/page-header"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { fetchSenderRelevance } from "@/data"
import type { SenderRelevance } from "@/data"
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

export function SenderRelevancePage() {
  const { data, loading, error, reload } = useAsync<SenderRelevance[]>(
    () => fetchSenderRelevance(),
    [],
  )
  const rows = data ?? []

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="categoría · calidad"
        title="Relevancia por remitente"
        description="Qué tan seguido cada remitente produjo un hecho de dominio (relevante) frente a solo leerse o quedar inerte (ruido). Determinista, sin LLM, con el ruido primero — es el insumo para decidir a quién dejar de procesar. El % cuenta solo los mensajes con un hecho extraído; 'solo lectura' e 'inertes' van aparte para no inflarlo."
      />
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
                <th className="px-3 py-2 font-medium">Tiers</th>
                <th className="px-3 py-2 font-medium">Último</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
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
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {tierMixLabel(r.tierMix)}
                  </td>
                  <td className="px-3 py-2 text-xs whitespace-nowrap text-muted-foreground">
                    {shortDate(r.lastAt)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
