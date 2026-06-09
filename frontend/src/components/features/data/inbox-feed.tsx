import { useEffect, useLayoutEffect, useMemo, useRef } from "react"
import { useNavigate, useSearchParams } from "react-router-dom"
import { useVirtualizer } from "@tanstack/react-virtual"
import { Inbox, Loader2, Search } from "lucide-react"
import { cn } from "@/lib/utils"
import { ATTACHMENT_ICON, ATTACHMENT_LABEL, type AttachmentKind } from "@/lib/attachment-kind"
import { Panel } from "@/components/common/panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Led } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { TierTag } from "@/components/common/tier-tag"
import { Input } from "@/components/ui/input"
import { fetchInbox, fetchInboxStats, fetchSources } from "@/data"
import { consumeFeedReturn, saveFeedReturn, type FeedReturnState } from "@/lib/feed-return"
import { useAsync } from "@/lib/use-async"
import { useAutoRefresh } from "@/state/auto-refresh"
import { dayLabel, initials, sourceMeta, summarizeRow } from "@/lib/inbox-format"
import { inboxStatus } from "@/lib/status"
import type { InboxRow, Source } from "@/types/domain"

const MAX = 2000
type SourceStats = Record<string, { total: number; pending: number; errored: number }>

type FeedItem =
  | { kind: "header"; key: string; label: string; count: number }
  | { kind: "row"; key: string; row: InboxRow; source?: Source }

export function InboxFeed() {
  const navigate = useNavigate()
  // Filtros EN LA URL (única fuente de verdad): sobreviven al ir-y-volver del detalle y el atajo
  // "?source=" de /carga sale gratis. Mutaciones con replace (no ensucian el historial) y en forma
  // funcional (la no-funcional captura el search del render y puede pisar params concurrentes).
  const [params, setSearchParams] = useSearchParams()
  const { now } = useAutoRefresh()
  const sourceId = params.get("source") ?? "all"
  const q = params.get("q") ?? ""
  const currentSearch = params.toString()
  const setParam = (key: "source" | "q", value: string | null) =>
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev)
        if (value) next.set(key, value)
        else next.delete(key) // limpiar = borrar la clave (sin "q=" ni "source=all" residuales)
        return next
      },
      { replace: true },
    )

  const { data: sources } = useAsync<Source[]>(() => fetchSources(), [])
  const { data: stats } = useAsync<{ sources: SourceStats }>(() => fetchInboxStats(), [])
  const sourceById = useMemo(() => {
    const m = new Map<number, Source>()
    for (const s of sources ?? []) m.set(s.id, s)
    return m
  }, [sources])

  const {
    data: rowsRaw,
    loading,
    error,
    reload,
  } = useAsync<InboxRow[]>(
    () => fetchInbox({ sourceId: sourceId === "all" ? undefined : Number(sourceId), max: MAX }),
    [sourceId],
  )

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return (rowsRaw ?? [])
      .filter((r) => {
        if (!needle) return true
        const s = summarizeRow(r)
        return `${s.sender} ${s.context} ${s.title} ${s.snippet}`.toLowerCase().includes(needle)
      })
      .sort((a, b) => new Date(b.occurredAt).getTime() - new Date(a.occurredAt).getTime())
  }, [rowsRaw, q])

  const items = useMemo<FeedItem[]>(() => {
    const out: FeedItem[] = []
    let curDay = ""
    let headerIdx = -1
    for (const row of rows) {
      const label = dayLabel(row.occurredAt, now)
      if (label !== curDay) {
        curDay = label
        out.push({ kind: "header", key: `h:${label}`, label, count: 0 })
        headerIdx = out.length - 1
      }
      if (headerIdx >= 0) (out[headerIdx] as { count: number }).count++
      out.push({ kind: "row", key: `r:${row.id}`, row, source: sourceById.get(row.sourceId) })
    }
    return out
  }, [rows, now, sourceById])

  const truncated = (rowsRaw?.length ?? 0) >= MAX

  const parentRef = useRef<HTMLDivElement>(null)
  const virt = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    // Estimación inicial; la altura real la mide measureElement (filas adaptativas al contenido).
    estimateSize: (i) => (items[i].kind === "header" ? 34 : 52),
    // Cachear la medición por la key estable del item (no por índice): al cambiar de fuente,
    // las alturas siguen al contenido en vez de reusar las de la vista anterior.
    getItemKey: (i) => items[i].key,
    overscan: 16,
  })
  const vItems = virt.getVirtualItems()

  // --- ida-y-vuelta al detalle: guardar el ancla al desmontar + restaurar one-shot ----------
  // El cleanup de desmontar no ve el último render → patrón "latest ref": un effect sin deps
  // refresca la closure en cada commit y el cleanup del effect [] la invoca al salir de la vista.
  const saveRef = useRef<(() => void) | null>(null)
  useEffect(() => {
    saveRef.current = () => {
      const el = parentRef.current
      // Sin data cargada no se pisa lo guardado (cubre además el desmontaje sintético de
      // StrictMode en dev, que ocurre antes de que llegue el primer fetch).
      if (!el || !rowsRaw) return
      const scrollTop = el.scrollTop
      const vi = vItems.find((v) => v.end > scrollTop)
      saveFeedReturn({
        search: currentSearch,
        anchorKey: vi ? (items[vi.index]?.key ?? null) : null,
        anchorDelta: vi ? scrollTop - vi.start : 0,
        scrollTop,
      })
    }
  })
  // useLayoutEffect a propósito: su cleanup corre en la fase de mutación, con parentRef AÚN
  // conectado; el de useEffect corre después de que React desconecta los refs (current=null)
  // y el guardado nunca ocurriría.
  useLayoutEffect(() => () => saveRef.current?.(), [])

  // Restauración: espera la primera data, consume el estado guardado (one-shot) y solo aplica si
  // el filtro coincide. Loop de rAF porque con alturas dinámicas un scrollToIndex único queda
  // corto (las filas reales se miden al montarse y los offsets se re-acomodan); se itera hasta
  // que el scroll se estabiliza y recién ahí se suma el delta dentro del ancla. `pendingRef` se
  // anula al COMPLETAR (no antes): el doble-effect de StrictMode cancela el primer loop y el
  // segundo lo reintenta desde el estado ya consumido en memoria.
  const pendingRef = useRef<FeedReturnState | null | undefined>(undefined)
  useEffect(() => {
    if (loading || !rowsRaw || items.length === 0) return
    if (pendingRef.current === undefined) pendingRef.current = consumeFeedReturn()
    const pending = pendingRef.current
    if (!pending) return
    if (pending.search !== currentSearch) {
      pendingRef.current = null
      return
    }
    const idx = pending.anchorKey ? items.findIndex((i) => i.key === pending.anchorKey) : -1
    let raf = 0
    let prevTop = Number.NaN
    let stable = 0
    let frames = 0
    const step = () => {
      // Ancla desaparecida (la data cambió entre visitas) → mejor esfuerzo por offset absoluto.
      if (idx >= 0) virt.scrollToIndex(idx, { align: "start" })
      else virt.scrollToOffset(pending.scrollTop)
      const top = parentRef.current?.scrollTop ?? 0
      stable = Math.abs(top - prevTop) <= 2 ? stable + 1 : 0
      prevTop = top
      frames += 1
      if (stable >= 2 || frames >= 24) {
        if (idx >= 0 && parentRef.current) parentRef.current.scrollTop = top + pending.anchorDelta
        pendingRef.current = null
        return
      }
      raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [loading, rowsRaw, items, currentSearch, virt])

  const rail = useMemo(() => {
    const s = stats?.sources ?? {}
    const total = Object.values(s).reduce((a, v) => a + v.total, 0)
    const list = Object.entries(s)
      .map(([id, v]) => ({ id: Number(id), count: v.total, source: sourceById.get(Number(id)) }))
      .sort((a, b) => b.count - a.count)
    return { total, list }
  }, [stats, sourceById])

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <SourceChip
          active={sourceId === "all"}
          onClick={() => setParam("source", null)}
          icon={Inbox}
          tone="text-brand"
          label="Todas"
          count={rail.total}
        />
        {rail.list.map(({ id, count, source }) => {
          const m = sourceMeta(source)
          return (
            <SourceChip
              key={id}
              active={sourceId === String(id)}
              onClick={() => setParam("source", String(id))}
              icon={m.icon}
              tone={m.tone}
              label={m.label}
              count={count}
            />
          )
        })}
      </div>

      {/* Altura ACOTADA al viewport: sin esto el panel crece al contenido y el que scrollea es
          <main> — el scroller interno (parentRef) queda muerto, la virtualización degenera en
          render-todo (2.000 filas en el DOM) y la restauración de scroll no tiene a quién apuntar.
          El calc resta topbar + paddings + header + chips (~278px); el min-h es el piso en
          ventanas chicas (ahí vuelve a scrollear main, comportamiento previo). */}
      <Panel className="flex h-[calc(100svh-17.5rem)] min-h-[520px] flex-col overflow-hidden">
        <div className="flex items-center gap-2 border-b border-border p-3">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              placeholder="Buscar remitente, asunto o texto…"
              value={q}
              onChange={(e) => setParam("q", e.target.value || null)}
              className="h-9 pl-8"
            />
          </div>
          <span className="num shrink-0 text-xs text-muted-foreground">
            {loading && !rowsRaw ? "cargando…" : `${rows.length}${truncated ? "+" : ""} mensajes`}
          </span>
        </div>

        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !rowsRaw ? (
          <div className="flex flex-1 items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Cargando inbox…
          </div>
        ) : items.length === 0 ? (
          <EmptyState title="Sin mensajes" hint="Probá otra fuente o trae correos en /carga." />
        ) : (
          <div ref={parentRef} className="flex-1 overflow-y-auto">
            <div style={{ height: virt.getTotalSize(), position: "relative", width: "100%" }}>
              {vItems.map((vi) => {
                const it = items[vi.index]
                return (
                  <div
                    key={it.key}
                    data-index={vi.index}
                    ref={virt.measureElement}
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      width: "100%",
                      transform: `translateY(${vi.start}px)`,
                    }}
                  >
                    {it.kind === "header" ? (
                      <div className="flex items-center gap-2 px-4 pb-1 pt-4">
                        <span className="eyebrow">{it.label}</span>
                        <span className="num text-[10px] text-muted-foreground">· {it.count}</span>
                        <div className="ml-1 h-px flex-1 bg-border" />
                      </div>
                    ) : (
                      <FeedRow row={it.row} source={it.source} onClick={() => navigate(`/datos/${it.row.id}`)} />
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </Panel>
    </div>
  )
}

function SourceChip({
  active,
  onClick,
  icon: Icon,
  tone,
  label,
  count,
}: {
  active: boolean
  onClick: () => void
  icon: React.ComponentType<{ className?: string }>
  tone: string
  label: string
  count: number
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-sm transition-colors",
        active ? "border-brand/50 bg-brand/10 text-foreground" : "border-border text-muted-foreground hover:bg-accent/40",
      )}
    >
      <Icon className={cn("size-3.5", tone)} />
      <span className="font-medium">{label}</span>
      <span className="num rounded-full bg-muted px-1.5 py-0.5 text-[10px] tabular-nums text-muted-foreground">
        {count.toLocaleString("es")}
      </span>
    </button>
  )
}

/** Íconos por tipo de adjunto (deduplicados) con tooltip. Hasta 4 + "+N". */
function AttachmentIcons({ kinds }: { kinds: AttachmentKind[] }) {
  if (kinds.length === 0) return null
  const shown = kinds.slice(0, 4)
  const extra = kinds.length - shown.length
  const title = `Adjuntos: ${kinds.map((k) => ATTACHMENT_LABEL[k]).join(", ")}`
  return (
    <span className="flex shrink-0 items-center gap-0.5 text-muted-foreground" title={title}>
      {shown.map((k, i) => {
        const Icon = ATTACHMENT_ICON[k]
        return <Icon key={i} className="size-3" />
      })}
      {extra > 0 && <span className="num text-[9px]">+{extra}</span>}
    </span>
  )
}

/** LED de estado de procesamiento del mensaje (sin procesar / procesado / error) con tooltip. */
function StatusDot({ row }: { row: InboxRow }) {
  const st = inboxStatus(row)
  return (
    <span title={st.label} className="flex shrink-0 items-center">
      <Led tone={st.tone} pulse={st.tone === "error"} size={7} />
    </span>
  )
}

/** Fila especializada por tipo: el correo se ve distinto al chat/social, y un mensaje corto ocupa
 * lo mínimo (altura adaptativa al contenido vía measureElement). */
function FeedRow({ row, source, onClick }: { row: InboxRow; source?: Source; onClick: () => void }) {
  const m = sourceMeta(source)
  const s = summarizeRow(row)
  const Icon = m.icon
  const cls = row.classification

  // Chat / social: compacto, una línea — avatar circular de iniciales + remitente + texto inline.
  if (s.kind === "chat" || s.kind === "social") {
    return (
      <button
        type="button"
        onClick={onClick}
        className="flex w-full items-center gap-2.5 border-b border-border px-4 py-1.5 text-left hover:bg-accent/40"
      >
        <StatusDot row={row} />
        <div
          className={cn(
            "num grid size-6 shrink-0 place-items-center rounded-full bg-muted text-[9px] font-semibold",
            m.tone,
          )}
        >
          {initials(s.sender)}
        </div>
        <span className={cn("shrink-0 text-xs font-medium", m.tone)}>{s.sender}</span>
        {s.context && (
          <span className="max-w-[12rem] shrink-0 truncate text-[11px] text-muted-foreground">
            · {s.context}
          </span>
        )}
        <span className="truncate text-sm text-foreground/85">
          {s.title || (s.hasMedia ? `[${s.mediaLabel}]` : "(mensaje)")}
        </span>
        {s.title && <AttachmentIcons kinds={s.attachmentKinds} />}
        {cls && <TierTag tier={cls.tier} />}
        <span className="num ml-auto shrink-0 pl-2 text-[10px] text-muted-foreground">
          <RelativeTime date={row.occurredAt} />
        </span>
      </button>
    )
  }

  // Correo / otros: avatar cuadrado con icono + remitente, asunto y (si hay) snippet.
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full items-start gap-3 border-b border-border px-4 py-2 text-left hover:bg-accent/40"
    >
      <div className="relative mt-0.5 grid size-8 shrink-0 place-items-center rounded-md bg-muted">
        <Icon className={cn("size-4", m.tone)} />
        {/* LED de estado anclado a la esquina del avatar (no ocupa ancho en la fila). */}
        <span className="absolute -right-0.5 -top-0.5 rounded-full bg-card p-px">
          <StatusDot row={row} />
        </span>
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="min-w-0 truncate text-sm font-medium">{s.sender}</span>
          {cls && <TierTag tier={cls.tier} />}
          <AttachmentIcons kinds={s.attachmentKinds} />
          <span className="num ml-auto shrink-0 text-[11px] text-muted-foreground">
            <RelativeTime date={row.occurredAt} />
          </span>
        </div>
        <div className="truncate text-sm text-foreground/90">{s.title}</div>
        {s.snippet && <div className="truncate text-xs text-muted-foreground">{s.snippet}</div>}
      </div>
    </button>
  )
}
