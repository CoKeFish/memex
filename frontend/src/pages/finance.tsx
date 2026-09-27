// /finanzas — en qué se va la plata, mes a mes. El periodo manda: se elige arriba (un mes con
// flechas, o 3/6/12 meses o todo) y de ahí cuelgan el resumen, la tira de meses (que además navega),
// las categorías y la lista de movimientos. Cada corte es un filtro: al tocar una categoría o una
// contraparte, la lista se queda con eso.
//
// Todo el recorte ocurre en el cliente sobre un único fetch (~1.600 movimientos): cambiar de mes es
// instantáneo. El periodo y los filtros viven en la URL, así una vista sobrevive al reload.
// Los ids, la evidencia cruda y la actividad del módulo quedan detrás de «Inspección».

import { useSearchParams } from "react-router-dom"
import { Loader2 } from "lucide-react"
import { PageHeader } from "@/components/common/page-header"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { Switch } from "@/components/ui/switch"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { useAsync } from "@/lib/use-async"
import { useInspectMode } from "@/lib/inspect-mode"
import { money } from "@/lib/money"
import {
  byCategory,
  coverage,
  EMPTY_FILTERS,
  monthKeyOfDate,
  monthInSentence,
  monthRange,
  monthSeries,
  monthsWithData,
  monthsWithStatement,
  pace,
  periodMonths,
  rowsInPeriod,
  shiftMonth,
  summarize,
  type MovementFilters,
  type Period,
  type PeriodMode,
} from "@/lib/finance-period"
import { CategoryBars } from "@/components/features/finance/category-bars"
import { FinanceActivity } from "@/components/features/finance/finance-activity"
import { FinanceSummary } from "@/components/features/finance/finance-summary"
import { MovementsList } from "@/components/features/finance/movements-list"
import { PeriodPicker } from "@/components/features/finance/period-picker"
import { RecurringPanel } from "@/components/features/finance/recurring-panel"
import { UploadStatement } from "@/components/features/finance/upload-statement"
import {
  fetchFinanceTransactions,
  fetchPendingPayments,
  fetchSubscriptions,
  financeCurrencies,
} from "@/data"
import type { FinanceTransaction } from "@/types/domain"

const MODES: PeriodMode[] = ["mes", "3m", "6m", "12m", "todo"]

/** Cobros fallidos o pendientes sin un pago posterior al mismo servicio: lo que falta regularizar.
 *  Va arriba de todo porque es lo único de esta pantalla que pide una acción. */
function PorRegularizar() {
  const { data } = useAsync(() => fetchPendingPayments(30), [])
  const rows = data ?? []
  if (rows.length === 0) return null
  return (
    <Panel>
      <PanelHeader
        eyebrow="por regularizar"
        title={`${rows.length} ${rows.length === 1 ? "cobro sin resolver" : "cobros sin resolver"}`}
        sub="Fallaron o quedaron pendientes y no hubo un pago posterior al mismo servicio en 30 días."
      />
      <PanelBody className="p-0">
        <ul className="divide-y divide-border/60">
          {rows.map((r) => (
            <li key={r.id} className="flex items-center gap-3 px-4 py-2 text-sm">
              <span className="min-w-0 flex-1 truncate font-medium">
                {r.counterparty || "(sin contraparte)"}
              </span>
              <StatusBadge tone={r.status === "fallida" ? "error" : "pending"} label={r.status} />
              <span className="num shrink-0">{money(r.amount, r.currency)}</span>
              <span className="num w-16 shrink-0 text-right text-[11px] text-muted-foreground">
                hace {r.ageDays} d
              </span>
            </li>
          ))}
        </ul>
      </PanelBody>
    </Panel>
  )
}

export function FinancePage() {
  const { data, loading, error, reload } = useAsync<FinanceTransaction[]>(
    () => fetchFinanceTransactions(),
    [],
  )
  const subs = useAsync(fetchSubscriptions, [])
  const [inspect, setInspect] = useInspectMode()
  const [params, setParams] = useSearchParams()
  // Sin useMemo: el recorte son unos filtros sobre ~1.600 filas y el compilador de React ya
  // memoriza lo que hace falta; memorizar a mano acá solo agregaba dependencias frágiles.
  const txns = data ?? []
  const currencies = financeCurrencies(txns)
  const currency = params.get("cur") ?? currencies[0] ?? "COP"
  const months = monthsWithData(txns, currency)
  const today = new Date()
  const latest = months[months.length - 1] ?? monthKeyOfDate(today)

  const mode = (params.get("p") as PeriodMode) ?? "mes"
  const period: Period = {
    mode: MODES.includes(mode) ? mode : "mes",
    month: params.get("m") ?? latest,
  }
  const filters: MovementFilters = {
    direction: params.get("dir") ?? EMPTY_FILTERS.direction,
    category: params.get("cat") ?? EMPTY_FILTERS.category,
    query: params.get("q") ?? "",
    includeUnsettled: params.get("nm") !== "0",
  }

  /** Escribe en la URL solo lo que no es el default: una URL corta se lee y se comparte. */
  function patch(next: Record<string, string | null>): void {
    const p = new URLSearchParams(params)
    for (const [k, v] of Object.entries(next)) {
      if (v === null || v === "") p.delete(k)
      else p.set(k, v)
    }
    setParams(p, { replace: true })
  }

  const setPeriod = (next: Period): void =>
    patch({ p: next.mode === "mes" ? null : next.mode, m: next.month === latest ? null : next.month })
  const setFilters = (f: MovementFilters): void =>
    patch({
      dir: f.direction === "all" ? null : f.direction,
      cat: f.category === "all" ? null : f.category,
      q: f.query || null,
      nm: f.includeUnsettled ? null : "0",
    })

  // Periodo → filas; de ahí salen resumen, categorías y lista. La tira siempre muestra los últimos
  // 12 meses terminados en el mes elegido (contexto estable aunque el periodo sea de un mes).
  const enPeriodo = periodMonths(period, months)
  const inPeriod = rowsInPeriod(txns, currency, enPeriodo)
  const summary = summarize(inPeriod)
  const cats = byCategory(inPeriod)
  const anchor = period.mode === "todo" ? latest : period.month
  const strip = monthSeries(txns, currency, monthRange(shiftMonth(anchor, -11), anchor))
  const ritmo = period.mode === "mes" ? pace(txns, currency, period.month, today) : null
  // ¿Al periodo le falta el extracto del banco? Un mes sin él solo tiene lo que llegó por
  // correo y la cifra queda corta; decirlo es la diferencia entre un dato y un dato creíble.
  const cobertura = coverage(enPeriodo, monthsWithStatement(txns, currency), today)

  // Completa «Gastos de …»: «septiembre», «los últimos 6 meses», «todo lo que hay».
  const periodLabel =
    period.mode === "todo"
      ? "todo lo que hay"
      : period.mode === "mes"
        ? monthInSentence(period.month, today)
        : `los últimos ${period.mode.replace("m", "")} meses`

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="Dominios"
        title="Finanzas"
        description="En qué se va la plata: mes a mes, por categoría y movimiento, con el mensaje del que salió cada cobro."
        actions={
          <>
            <UploadStatement onDone={reload} />
            {currencies.length > 1 && (
              <Select value={currency} onValueChange={(v) => patch({ cur: v })}>
                <SelectTrigger size="sm" className="w-24 text-xs" aria-label="Moneda">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {currencies.map((c) => (
                    <SelectItem key={c} value={c} className="text-xs">
                      {c}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            <label
              className="inline-flex cursor-pointer items-center gap-2 text-xs text-muted-foreground"
              title="Muestra de qué mensaje salió cada movimiento, su texto original y los ids"
            >
              <Switch
                size="sm"
                checked={inspect}
                onCheckedChange={setInspect}
                aria-label="Modo inspección"
              />
              Inspección
            </label>
          </>
        }
      />

      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading && !data ? (
        <div className="flex items-center justify-center gap-2 py-24 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Cargando movimientos…
        </div>
      ) : txns.length === 0 ? (
        <>
          <EmptyState
            title="Sin movimientos"
            hint="Todavía no se extrajo ningún cobro de los mensajes. Abajo, la actividad del módulo dice si corrió y qué pasó."
          />
          <FinanceActivity />
        </>
      ) : (
        <>
          <PorRegularizar />

          <PeriodPicker period={period} months={months} onChange={setPeriod} />

          <FinanceSummary
            summary={summary}
            series={strip}
            selectedMonths={enPeriodo}
            currency={currency}
            pace={ritmo}
            periodLabel={periodLabel}
            coverage={cobertura}
            today={today}
            onSelectMonth={(m) => patch({ m: m === latest ? null : m, p: null })}
          />

          <div className="grid gap-5 xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)]">
            <div className="space-y-5">
              <Panel>
                <PanelHeader
                  eyebrow="en qué se fue"
                  title={money(summary.expense, currency)}
                  sub="Tocá una categoría para ver solo esos movimientos."
                />
                <PanelBody className="p-2">
                  <CategoryBars
                    rows={cats}
                    currency={currency}
                    active={filters.category}
                    onPick={(c) => setFilters({ ...filters, category: c })}
                  />
                </PanelBody>
              </Panel>
              <RecurringPanel
                items={subs.data?.items ?? []}
                commitment={subs.data?.commitment ?? []}
              />
            </div>

            <MovementsList
              rows={inPeriod}
              currency={currency}
              filters={filters}
              onFilters={setFilters}
              inspect={inspect}
              onChanged={reload}
            />
          </div>

          {inspect ? (
            <FinanceActivity />
          ) : (
            <p className="text-[11px] text-muted-foreground">
              La inspección (de qué mensaje salió cada cobro, su texto original, la actividad del
              módulo) está apagada.{" "}
              <button
                type="button"
                onClick={() => setInspect(true)}
                className="underline-offset-2 hover:underline"
              >
                Encender
              </button>
            </p>
          )}
        </>
      )}
    </div>
  )
}
