// Los movimientos del periodo, agrupados por día (como el extracto de un banco) o en una lista
// plana cuando se ordena por monto («¿cuál fue el gasto más grande?»). El color dice qué pasó con
// la plata, sin gritar: un gasto va en tinta normal —son el 90 % de las filas y una tabla toda roja
// no se lee—, lo que entró en verde, lo devuelto en azul, y lo que NO se movió (fallido o
// pendiente) tachado y apagado. Los ids y la evidencia cruda viven detrás de «Inspección».

import { useState } from "react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { ArrowDownWideNarrow, CalendarDays, MapPin, Search } from "lucide-react"
import { cn } from "@/lib/utils"
import { money } from "@/lib/money"
import { CATEGORIES, CATEGORY_CHART, CATEGORY_LABEL } from "@/lib/finance"
import { applyFilters, type MovementFilters } from "@/lib/finance-period"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { EmptyState } from "@/components/common/data-state"
import { setFinanceRubro } from "@/data/finance"
import type { ExpenseCategory, FinanceTransaction } from "@/types/domain"

const PAGE = 25

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

const WEEKDAYS = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"]
const MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

/** «miércoles 12 de septiembre» a partir de `YYYY-MM-DD`, sin correrse de día por la zona. */
function dayHeading(iso: string): string {
  const d = new Date(`${iso}T12:00:00`)
  return `${WEEKDAYS[d.getDay()]} ${d.getDate()} de ${MONTHS[d.getMonth()]}`
}

function shortDay(iso: string): string {
  const d = new Date(`${iso}T12:00:00`)
  return `${d.getDate()} ${MONTHS[d.getMonth()]}`
}

function Amount({ t, currency }: { t: FinanceTransaction; currency: string }) {
  const settled = t.status === "completada"
  const refund = t.kind === "reembolso"
  const income = t.direction === "ingreso"
  return (
    <span
      className={cn(
        "num shrink-0 text-sm tabular-nums",
        !settled && "text-muted-foreground line-through decoration-muted-foreground/50",
        settled && refund && "text-chart-2",
        settled && income && !refund && "text-status-ok",
      )}
      title={
        settled
          ? refund
            ? "Plata que volvió: descuenta del gasto"
            : income
              ? "Entró"
              : "Salió"
          : t.status === "fallida"
            ? "El cobro falló: no se movió plata"
            : "Pendiente: todavía no se movió"
      }
    >
      {income ? "+" : ""}
      {money(t.amount, currency)}
    </span>
  )
}

//: El cuadrito de color de la fila es también el selector de rubro: elegir uno ahí guarda la
//: regla «esta contraparte es <rubro>» y se aplica a todo lo suyo, pasado y futuro. Sin
//: contraparte no hay regla posible, así que el cuadrito queda mudo.
function RubroPicker({
  t,
  onRubro,
}: {
  t: FinanceTransaction
  onRubro: (counterparty: string, category: ExpenseCategory) => void
}) {
  const label = CATEGORY_LABEL[t.category] ?? t.category
  const square = (
    <span
      aria-hidden
      className="size-2.5 shrink-0 rounded-[3px]"
      style={{ background: CATEGORY_CHART[t.category] ?? "var(--status-filtered)" }}
    />
  )
  if (!t.counterparty) {
    return (
      <span className="mt-1.5 inline-flex" title={label}>
        {square}
      </span>
    )
  }
  return (
    <Select
      value={t.category}
      onValueChange={(v) => {
        if (v !== t.category) onRubro(t.counterparty, v as ExpenseCategory)
      }}
    >
      <SelectTrigger
        aria-label={`Rubro: ${label}. Cambiar el rubro de ${t.counterparty}`}
        title={`${label} · cambiar el rubro de ${t.counterparty}`}
        className="mt-1 h-auto w-auto gap-0 rounded-sm border-0 bg-transparent p-0.5 shadow-none hover:ring-2 hover:ring-ring/40 focus:ring-2 focus:ring-ring [&>svg]:hidden"
      >
        {square}
      </SelectTrigger>
      <SelectContent align="start">
        {CATEGORIES.map((c) => (
          <SelectItem key={c.key} value={c.key}>
            <span className="inline-flex items-center gap-2">
              <span aria-hidden className="size-2.5 rounded-[3px]" style={{ background: c.chart }} />
              {c.label}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function Row({
  t,
  currency,
  inspect,
  showDate,
  onPickMerchant,
  onRubro,
}: {
  t: FinanceTransaction
  currency: string
  inspect: boolean
  showDate: boolean
  onPickMerchant: (m: string) => void
  onRubro: (counterparty: string, category: ExpenseCategory) => void
}) {
  const lugar = [t.placeName, t.placeAddress].filter(Boolean).join(" — ")
  return (
    <li className="flex items-start gap-3 border-b border-border/50 px-2.5 py-2 last:border-0 hover:bg-muted/30">
      {showDate && (
        <span className="num w-14 shrink-0 pt-0.5 text-[11px] text-muted-foreground">
          {shortDay(t.occurredOn)}
        </span>
      )}
      <RubroPicker t={t} onRubro={onRubro} />
      <span className="min-w-0 flex-1">
        <button
          type="button"
          onClick={() => onPickMerchant(t.merchant)}
          className="max-w-full truncate text-left text-sm font-medium hover:underline"
          title={`Ver todo lo de ${t.merchant}`}
        >
          {t.merchant}
        </button>
        <span className="block truncate text-[11px] text-muted-foreground">
          {t.description || CATEGORY_LABEL[t.category] || ""}
          {t.kind === "reembolso" && " · devolución"}
          {t.status !== "completada" && ` · ${t.status}`}
        </span>
        {lugar && (
          <span className="flex items-center gap-1 text-[11px] text-muted-foreground">
            <MapPin className="size-3 shrink-0" />
            <span className="truncate" title={lugar}>
              {lugar}
            </span>
          </span>
        )}
        {inspect && (
          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-muted-foreground/80">
            {t.evidence && (
              <span className="truncate" title={t.evidence}>
                «{t.evidence}»
              </span>
            )}
            {t.sourceInboxIds.slice(0, 3).map((id) => (
              <Link key={id} to={`/datos/${id}`} className="num text-origin-inbox hover:underline">
                #{id}
              </Link>
            ))}
            <span className="num">mov #{t.id}</span>
          </span>
        )}
      </span>
      <Amount t={t} currency={currency} />
    </li>
  )
}

export function MovementsList({
  rows,
  currency,
  filters,
  onFilters,
  inspect,
  onChanged,
}: {
  /** Ya recortadas al periodo y a la moneda. */
  rows: FinanceTransaction[]
  currency: string
  filters: MovementFilters
  onFilters: (f: MovementFilters) => void
  inspect: boolean
  /** Después de cambiar un rubro (la regla se aplicó a todo lo de esa contraparte): recargar. */
  onChanged?: () => void
}) {
  const [sort, setSort] = useState<"fecha" | "monto">("fecha")
  const [page, setPage] = useState(0)

  const filtered = applyFilters(rows, filters)
  const sorted = [...filtered].sort((a, b) =>
    sort === "monto"
      ? b.amount - a.amount
      : b.occurredOn.localeCompare(a.occurredOn) || b.id - a.id,
  )
  const pages = Math.max(1, Math.ceil(sorted.length / PAGE))
  const safe = Math.min(page, pages - 1)
  const view = sorted.slice(safe * PAGE, safe * PAGE + PAGE)

  async function onRubro(counterparty: string, category: ExpenseCategory): Promise<void> {
    try {
      const r = await setFinanceRubro(counterparty, category)
      toast.success(`${counterparty} → ${CATEGORY_LABEL[category]}`, {
        description: r.applied === 1 ? "1 pago actualizado" : `${r.applied} pagos actualizados`,
      })
      onChanged?.()
    } catch (e) {
      toast.error(`Rubro: ${errMsg(e)}`)
    }
  }

  const set = (patch: Partial<MovementFilters>): void => {
    setPage(0)
    onFilters({ ...filters, ...patch })
  }

  // Por fecha, las filas se agrupan bajo el día (como un extracto); por monto es una lista plana y
  // cada fila lleva su fecha.
  const groups: { day: string; items: FinanceTransaction[] }[] = []
  if (sort === "fecha") {
    for (const t of view) {
      const last = groups[groups.length - 1]
      if (last && last.day === t.occurredOn) last.items.push(t)
      else groups.push({ day: t.occurredOn, items: [t] })
    }
  }

  return (
    <section className="flex min-h-0 flex-col rounded-lg border border-border bg-card">
      <header className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2.5">
        <h2 className="mr-auto text-[13px] font-semibold">
          Movimientos{" "}
          <span className="num font-normal text-muted-foreground">{sorted.length}</span>
        </h2>
        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={filters.query}
            onChange={(e) => set({ query: e.target.value })}
            placeholder="Buscar contraparte o detalle"
            aria-label="Buscar movimientos"
            className="h-8 w-52 pl-7 text-xs"
          />
        </div>
        <button
          type="button"
          onClick={() => {
            setSort((s) => (s === "fecha" ? "monto" : "fecha"))
            setPage(0)
          }}
          title={sort === "fecha" ? "Ordenar por monto" : "Ordenar por fecha"}
          className="inline-flex h-8 items-center gap-1.5 rounded-md border border-input px-2.5 text-xs text-muted-foreground hover:bg-muted/50 hover:text-foreground"
        >
          {sort === "fecha" ? (
            <CalendarDays className="size-3.5" />
          ) : (
            <ArrowDownWideNarrow className="size-3.5" />
          )}
          {sort === "fecha" ? "Por fecha" : "Por monto"}
        </button>
      </header>

      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-2">
        <Select value={filters.direction} onValueChange={(v) => set({ direction: v })}>
          <SelectTrigger size="sm" className="w-32 text-xs" aria-label="Entró o salió">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all" className="text-xs">
              Todo
            </SelectItem>
            <SelectItem value="egreso" className="text-xs">
              Salió
            </SelectItem>
            <SelectItem value="ingreso" className="text-xs">
              Entró
            </SelectItem>
          </SelectContent>
        </Select>
        <Select value={filters.category} onValueChange={(v) => set({ category: v })}>
          <SelectTrigger size="sm" className="w-40 text-xs" aria-label="Categoría">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all" className="text-xs">
              Toda categoría
            </SelectItem>
            {CATEGORIES.map((c) => (
              <SelectItem key={c.key} value={c.key} className="text-xs">
                {c.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={filters.includeUnsettled}
            onChange={(e) => set({ includeUnsettled: e.target.checked })}
            className="size-3.5 accent-[var(--brand)]"
          />
          Incluir lo que no se movió
        </label>
        {(filters.query || filters.category !== "all" || filters.direction !== "all") && (
          <Button
            size="xs"
            variant="ghost"
            className="ml-auto"
            onClick={() =>
              set({ query: "", category: "all", direction: "all" })
            }
          >
            Limpiar filtros
          </Button>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {sorted.length === 0 ? (
          <EmptyState
            title="Sin movimientos"
            hint="No hay nada con estos filtros en el periodo elegido."
          />
        ) : sort === "fecha" ? (
          <div>
            {groups.map((g) => (
              <div key={g.day}>
                <h3 className="sticky top-0 z-10 bg-card/95 px-4 py-1.5 text-[11px] font-medium capitalize text-muted-foreground backdrop-blur">
                  {dayHeading(g.day)}
                </h3>
                <ul className="px-1.5">
                  {g.items.map((t) => (
                    <Row
                      key={t.id}
                      t={t}
                      currency={currency}
                      inspect={inspect}
                      showDate={false}
                      onPickMerchant={(m) => set({ query: m })}
                      onRubro={(cp, c) => void onRubro(cp, c)}
                    />
                  ))}
                </ul>
              </div>
            ))}
          </div>
        ) : (
          <ul className="px-1.5 pt-1">
            {view.map((t) => (
              <Row
                key={t.id}
                t={t}
                currency={currency}
                inspect={inspect}
                showDate
                onPickMerchant={(m) => set({ query: m })}
                onRubro={(cp, c) => void onRubro(cp, c)}
              />
            ))}
          </ul>
        )}
      </div>

      {pages > 1 && (
        <footer className="flex items-center justify-between border-t border-border px-4 py-2 text-xs text-muted-foreground">
          <span className="num">
            {safe * PAGE + 1}–{Math.min(sorted.length, safe * PAGE + PAGE)} de {sorted.length}
          </span>
          <span className="flex items-center gap-2">
            <Button
              size="xs"
              variant="outline"
              disabled={safe === 0}
              onClick={() => setPage(safe - 1)}
            >
              Anterior
            </Button>
            <span className="num">
              {safe + 1}/{pages}
            </span>
            <Button
              size="xs"
              variant="outline"
              disabled={safe >= pages - 1}
              onClick={() => setPage(safe + 1)}
            >
              Siguiente
            </Button>
          </span>
        </footer>
      )}
    </section>
  )
}
