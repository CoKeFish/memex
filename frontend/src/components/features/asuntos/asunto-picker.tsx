// Elegir otro asunto por su nombre (fusionar, relacionar) en vez de escribir su id: busca con la
// API mientras se escribe y muestra hasta 8 coincidencias con su tipo y estado.

import { useEffect, useState } from "react"
import { Search, X } from "lucide-react"
import { cn } from "@/lib/utils"
import { ASUNTO_KIND_LABEL, ASUNTO_STATUS_LABEL, ASUNTO_STATUSES } from "@/lib/asuntos"
import { Input } from "@/components/ui/input"
import { fetchAsuntos, type Asunto } from "@/data"

export function AsuntoPicker({
  value,
  onChange,
  excludeId,
  placeholder = "Buscar asunto por nombre…",
  autoFocus = false,
}: {
  value: Asunto | null
  onChange: (a: Asunto | null) => void
  /** El asunto actual no se ofrece (no tiene sentido fusionarse o relacionarse consigo mismo). */
  excludeId?: number
  placeholder?: string
  autoFocus?: boolean
}) {
  const [q, setQ] = useState("")
  // Los resultados van atados a la búsqueda que los produjo: así «buscando» y «sin coincidencias»
  // se derivan sin setState dentro del efecto (solo en el callback del fetch).
  const [found, setFound] = useState<{ q: string; items: Asunto[] }>({ q: "", items: [] })
  const needle = q.trim()
  const active = needle.length >= 2
  const results = found.q === needle ? found.items : []
  const searching = active && found.q !== needle

  useEffect(() => {
    if (needle.length < 2) return
    let cancelled = false
    const id = window.setTimeout(() => {
      fetchAsuntos({ q: needle, statuses: ASUNTO_STATUSES, limit: 8 })
        .then((r) => {
          if (!cancelled) setFound({ q: needle, items: r.items.filter((a) => a.id !== excludeId) })
        })
        .catch(() => {
          if (!cancelled) setFound({ q: needle, items: [] })
        })
    }, 250)
    return () => {
      cancelled = true
      window.clearTimeout(id)
    }
  }, [needle, excludeId])

  if (value) {
    return (
      <div className="flex items-center justify-between gap-2 rounded-md border border-border bg-muted/30 px-3 py-2 text-sm">
        <span className="min-w-0 truncate">
          <span className="font-medium">{value.name}</span>
          <span className="ml-2 text-xs text-muted-foreground">
            {ASUNTO_KIND_LABEL[value.kind] ?? value.kind} · {ASUNTO_STATUS_LABEL[value.status] ?? value.status}
          </span>
        </span>
        <button
          type="button"
          onClick={() => onChange(null)}
          className="rounded p-0.5 text-muted-foreground hover:bg-muted hover:text-foreground"
          aria-label="Elegir otro asunto"
          title="Elegir otro asunto"
        >
          <X className="size-3.5" />
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-1.5">
      <div className="relative">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={placeholder}
          className="h-9 pl-8 text-sm"
          autoFocus={autoFocus}
          aria-label="Buscar asunto"
        />
      </div>
      {active && (
        <ul className="max-h-56 overflow-y-auto rounded-md border border-border bg-popover text-sm" role="listbox">
          {results.length === 0 && (
            <li className="px-3 py-2 text-xs text-muted-foreground">
              {searching ? "Buscando…" : "Ningún asunto coincide."}
            </li>
          )}
          {results.map((a) => (
            <li key={a.id} role="option" aria-selected={false}>
              <button
                type="button"
                onClick={() => onChange(a)}
                className={cn(
                  "flex w-full items-center justify-between gap-3 px-3 py-1.5 text-left hover:bg-muted/60 focus-visible:bg-muted/60 focus-visible:outline-none",
                )}
              >
                <span className="min-w-0 truncate">{a.name}</span>
                <span className="shrink-0 text-[11px] text-muted-foreground">
                  {ASUNTO_KIND_LABEL[a.kind] ?? a.kind} · {ASUNTO_STATUS_LABEL[a.status] ?? a.status}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
