import { useMemo, useState } from "react"
import { ArrowDown, ArrowUp, Search } from "lucide-react"
import { Link } from "react-router-dom"
import { cn } from "@/lib/utils"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Button } from "@/components/ui/button"
import { EmptyState, Stateful, TableSkeleton } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { formatDateOnly, formatMoney } from "@/lib/format"
import { CATEGORIES, CATEGORY_CHART, CATEGORY_LABEL } from "@/data"
import type { ExpenseCategory, FinanceTransaction } from "@/types/domain"

const PAGE = 14

function CategoryChip({ category }: { category: ExpenseCategory }) {
  // Las categorías son de GASTO; un ingreso suele caer a 'otros'. Guard por si llega una clave que
  // no esté en el catálogo (no romper el render).
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span
        className="size-2 rounded-[3px]"
        style={{ background: CATEGORY_CHART[category] ?? "var(--status-filtered)" }}
      />
      {CATEGORY_LABEL[category] ?? category}
    </span>
  )
}

export function MovementsTable({ txns, currency }: { txns: FinanceTransaction[]; currency: string }) {
  const all = useMemo(() => txns.filter((t) => t.currency === currency), [txns, currency])
  const [direction, setDirection] = useState<"all" | "ingreso" | "egreso">("all")
  const [category, setCategory] = useState("all")
  const [query, setQuery] = useState("")
  const [sort, setSort] = useState<{ key: "occurredOn" | "amount"; dir: "asc" | "desc" }>({ key: "occurredOn", dir: "desc" })
  const [page, setPage] = useState(0)

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    const r = all.filter((t) => {
      if (direction !== "all" && t.direction !== direction) return false
      if (category !== "all" && t.category !== category) return false
      if (q && !`${t.merchant} ${t.description} ${t.evidence}`.toLowerCase().includes(q)) return false
      return true
    })
    r.sort((a, b) => {
      const av = sort.key === "amount" ? a.amount : new Date(a.occurredOn).getTime()
      const bv = sort.key === "amount" ? b.amount : new Date(b.occurredOn).getTime()
      return sort.dir === "asc" ? av - bv : bv - av
    })
    return r
  }, [all, direction, category, query, sort])

  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE))
  const safePage = Math.min(page, pageCount - 1)
  const view = rows.slice(safePage * PAGE, safePage * PAGE + PAGE)
  // Neto: los egresos restan, los ingresos suman (los montos en la DB son positivos; el signo es de
  // presentación). Da el balance del subconjunto filtrado.
  const net = rows.reduce((a, t) => a + (t.direction === "egreso" ? -t.amount : t.amount), 0)

  function toggleSort(key: "occurredOn" | "amount") {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }))
    setPage(0)
  }

  return (
    <Panel>
      <PanelHeader
        eyebrow="finanzas · movimientos"
        title="Movimientos"
        sub={`${rows.length} movimientos · neto ${formatMoney(net, currency)}`}
        right={
          <div className="relative">
            <Search className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input value={query} onChange={(e) => { setQuery(e.target.value); setPage(0) }} placeholder="contraparte / evidencia" className="h-8 w-48 pl-7 text-xs" />
          </div>
        }
      />
      <div className="flex flex-wrap gap-2 border-b border-border px-4 py-2.5">
        <Select value={direction} onValueChange={(v) => { setDirection(v as "all" | "ingreso" | "egreso"); setPage(0) }}>
          <SelectTrigger className="h-8 w-auto min-w-[120px] text-xs" aria-label="Tipo de movimiento">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all" className="text-xs">Todo movimiento</SelectItem>
            <SelectItem value="ingreso" className="text-xs">Ingresos</SelectItem>
            <SelectItem value="egreso" className="text-xs">Gastos</SelectItem>
          </SelectContent>
        </Select>
        <Select value={category} onValueChange={(v) => { setCategory(v); setPage(0) }}>
          <SelectTrigger className="h-8 w-auto min-w-[140px] text-xs" aria-label="Categoría">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all" className="text-xs">Toda categoría</SelectItem>
            {CATEGORIES.map((c) => (
              <SelectItem key={c.key} value={c.key} className="text-xs">{c.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <PanelBody className="p-0">
        <Stateful skeleton={<TableSkeleton rows={PAGE} cols={5} />} empty={<EmptyState title="Sin movimientos" hint="El módulo finance aún no extrajo nada." />}>
          {view.length === 0 ? (
            <EmptyState title="Sin coincidencias" hint="Ajustá los filtros." />
          ) : (
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border bg-muted/30 text-left">
                  <th className="px-3 py-2 font-medium text-muted-foreground">
                    <button onClick={() => toggleSort("occurredOn")} className="inline-flex items-center gap-1 hover:text-foreground">
                      Fecha {sort.key === "occurredOn" && (sort.dir === "asc" ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
                    </button>
                  </th>
                  <th className="px-3 py-2 font-medium text-muted-foreground">Contraparte</th>
                  <th className="px-3 py-2 font-medium text-muted-foreground">Categoría</th>
                  <th className="px-3 py-2 text-right font-medium text-muted-foreground">
                    <button onClick={() => toggleSort("amount")} className="inline-flex items-center gap-1 hover:text-foreground">
                      Monto {sort.key === "amount" && (sort.dir === "asc" ? <ArrowUp className="size-3" /> : <ArrowDown className="size-3" />)}
                    </button>
                  </th>
                  <th className="px-3 py-2 font-medium text-muted-foreground">Origen</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {view.map((t) => (
                  <tr key={t.id} className="hover:bg-accent/30">
                    <td className="num whitespace-nowrap px-3 py-2 text-muted-foreground">{formatDateOnly(t.occurredOn)}</td>
                    <td className="px-3 py-2">
                      <div className="font-medium">{t.merchant}</div>
                      <div className="truncate text-[11px] text-muted-foreground" title={t.evidence}>{t.evidence}</div>
                    </td>
                    <td className="px-3 py-2"><CategoryChip category={t.category} /></td>
                    <td className={cn("num px-3 py-2 text-right font-medium", t.direction === "egreso" ? "text-status-error" : "text-status-ok")}>
                      {t.direction === "egreso" ? "−" : "+"}{formatMoney(t.amount, currency)}
                    </td>
                    <td className="num px-3 py-2">
                      {t.sourceInboxIds.length > 0 ? (
                        <Link to={`/datos/${t.sourceInboxIds[0]}`} className="text-origin-inbox hover:underline">
                          #{t.sourceInboxIds[0]}
                        </Link>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Stateful>
      </PanelBody>
      <div className="flex items-center justify-between border-t border-border px-4 py-2.5 text-xs text-muted-foreground">
        <span className="num">
          {rows.length === 0 ? 0 : safePage * PAGE + 1}–{Math.min(rows.length, safePage * PAGE + PAGE)} de {rows.length}
        </span>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" className="h-7" disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>Anterior</Button>
          <span className={cn("num")}>{safePage + 1}/{pageCount}</span>
          <Button variant="outline" size="sm" className="h-7" disabled={safePage >= pageCount - 1} onClick={() => setPage(safePage + 1)}>Siguiente</Button>
        </div>
      </div>
    </Panel>
  )
}
