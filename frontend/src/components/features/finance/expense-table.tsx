import { useMemo, useState } from "react"
import { ArrowDown, ArrowUp, Search } from "lucide-react"
import { Link } from "react-router-dom"
import { cn } from "@/lib/utils"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Button } from "@/components/ui/button"
import { EmptyState, Stateful, TableSkeleton } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { formatDate, formatMoney } from "@/lib/format"
import { CATEGORIES, CATEGORY_CHART, CATEGORY_LABEL } from "@/data"
import type { ExpenseCategory, FinanceExpense } from "@/types/domain"

const PAGE = 14

function CategoryChip({ category }: { category: ExpenseCategory }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs">
      <span className="size-2 rounded-[3px]" style={{ background: CATEGORY_CHART[category] }} />
      {CATEGORY_LABEL[category]}
    </span>
  )
}

export function ExpenseTable({ expenses, currency }: { expenses: FinanceExpense[]; currency: string }) {
  const all = useMemo(() => expenses.filter((e) => e.currency === currency), [expenses, currency])
  const [category, setCategory] = useState("all")
  const [query, setQuery] = useState("")
  const [sort, setSort] = useState<{ key: "occurredOn" | "amount"; dir: "asc" | "desc" }>({ key: "occurredOn", dir: "desc" })
  const [page, setPage] = useState(0)

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    const r = all.filter((e) => {
      if (category !== "all" && e.category !== category) return false
      if (q && !`${e.merchant} ${e.description} ${e.evidence}`.toLowerCase().includes(q)) return false
      return true
    })
    r.sort((a, b) => {
      const av = sort.key === "amount" ? a.amount : new Date(a.occurredOn).getTime()
      const bv = sort.key === "amount" ? b.amount : new Date(b.occurredOn).getTime()
      return sort.dir === "asc" ? av - bv : bv - av
    })
    return r
  }, [all, category, query, sort])

  const pageCount = Math.max(1, Math.ceil(rows.length / PAGE))
  const safePage = Math.min(page, pageCount - 1)
  const view = rows.slice(safePage * PAGE, safePage * PAGE + PAGE)
  const total = rows.reduce((a, e) => a + e.amount, 0)

  function toggleSort(key: "occurredOn" | "amount") {
    setSort((s) => (s.key === key ? { key, dir: s.dir === "asc" ? "desc" : "asc" } : { key, dir: "desc" }))
    setPage(0)
  }

  return (
    <Panel>
      <PanelHeader
        eyebrow="finanzas · gastos"
        title="Movimientos"
        sub={`${rows.length} gastos · ${formatMoney(total, currency)} en total`}
        right={
          <div className="relative">
            <Search className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input value={query} onChange={(e) => { setQuery(e.target.value); setPage(0) }} placeholder="comercio / evidencia" className="h-8 w-48 pl-7 text-xs" />
          </div>
        }
      />
      <div className="flex flex-wrap gap-2 border-b border-border px-4 py-2.5">
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
        <Stateful skeleton={<TableSkeleton rows={PAGE} cols={5} />} empty={<EmptyState title="Sin gastos" hint="El módulo finance aún no extrajo nada." />}>
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
                  <th className="px-3 py-2 font-medium text-muted-foreground">Comercio</th>
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
                {view.map((e) => (
                  <tr key={e.id} className="hover:bg-accent/30">
                    <td className="num whitespace-nowrap px-3 py-2 text-muted-foreground">{formatDate(e.occurredOn)}</td>
                    <td className="px-3 py-2">
                      <div className="font-medium">{e.merchant}</div>
                      <div className="truncate text-[11px] text-muted-foreground" title={e.evidence}>{e.evidence}</div>
                    </td>
                    <td className="px-3 py-2"><CategoryChip category={e.category} /></td>
                    <td className="num px-3 py-2 text-right font-medium">{formatMoney(e.amount, currency)}</td>
                    <td className="num px-3 py-2">
                      <Link to={`/datos/${e.sourceInboxIds[0]}`} className="text-origin-inbox hover:underline">
                        #{e.sourceInboxIds[0]}
                      </Link>
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
