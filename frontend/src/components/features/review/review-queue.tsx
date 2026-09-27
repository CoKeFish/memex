import { useMemo, useState } from "react"
import { Bug, CalendarClock, Loader2, RotateCcw } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { EmptyState } from "@/components/common/data-state"
import { Panel } from "@/components/common/panel"
import { Led } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { formatDate } from "@/lib/format"
import type { Tone } from "@/lib/status"
import { useAsync } from "@/lib/use-async"
import { fetchReviewItems, requeueDeadLetter } from "@/data/review"
import type {
  CalendarConflict,
  ConsolidatedEventLite,
  ReviewItem,
  ReviewKind,
  WorkItemFailure,
} from "@/types/domain"

// `dedup` se administra desde /calendar (su decisión es un slice de ese módulo); la cola muestra
// dead-letter (accionable: reencolar) y conflictos de calendario (solo-lectura por ahora).
const KIND_META: Record<ReviewKind, { label: string; icon: typeof Bug; tone: Tone }> = {
  "dead-letter": { label: "Dead-letter", icon: Bug, tone: "error" },
  conflict: { label: "Conflicto", icon: CalendarClock, tone: "review" },
  dedup: { label: "Dedup", icon: CalendarClock, tone: "running" },
}

const TABS: { value: "all" | ReviewKind; label: string }[] = [
  { value: "all", label: "Todos" },
  { value: "dead-letter", label: "Dead-letter" },
  { value: "conflict", label: "Conflictos" },
]

function timeLabel(start: string | null, end: string | null): string {
  if (!start) return "todo el día"
  return end ? `${start}–${end}` : start
}

export function ReviewQueue() {
  const { data, loading, error, reload } = useAsync(fetchReviewItems, [])
  const items = useMemo(() => data ?? [], [data])
  const [tab, setTab] = useState<"all" | ReviewKind>("all")
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: items.length, "dead-letter": 0, conflict: 0 }
    for (const it of items) c[it.kind] = (c[it.kind] ?? 0) + 1
    return c
  }, [items])

  const visible = tab === "all" ? items : items.filter((i) => i.kind === tab)
  const selected = items.find((i) => i.id === selectedId) ?? visible[0] ?? null

  async function onRequeue(dl: WorkItemFailure) {
    try {
      await requeueDeadLetter(dl.stage, dl.inboxId)
      toast.success("Mensaje reencolado", {
        description: "Vuelve al work-set; se reintenta en la próxima corrida.",
      })
      setSelectedId(null)
      await reload()
    } catch {
      toast.error("No se pudo reencolar")
    }
  }

  if (loading && !data) {
    return (
      <div className="flex items-center gap-2 p-4 text-xs text-muted-foreground">
        <Loader2 className="size-3 animate-spin" /> cargando…
      </div>
    )
  }
  if (error) return <p className="p-4 text-sm text-status-error">{error}</p>

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(320px,1fr)_minmax(0,1.4fr)]">
      <Panel className="flex flex-col overflow-hidden">
        <div className="border-b border-border p-2">
          <Tabs value={tab} onValueChange={(v) => setTab(v as typeof tab)}>
            <TabsList className="grid w-full grid-cols-3">
              {TABS.map((t) => (
                <TabsTrigger key={t.value} value={t.value} className="text-xs">
                  {t.label} · {counts[t.value] ?? 0}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>
        <ul className="max-h-[620px] flex-1 divide-y divide-border overflow-y-auto">
          {visible.length === 0 && (
            <EmptyState title="Nada pendiente aquí" hint="Bandeja vacía para este filtro." />
          )}
          {visible.map((it) => {
            const meta = KIND_META[it.kind]
            return (
              <li key={it.id}>
                <button
                  onClick={() => setSelectedId(it.id)}
                  className={cn(
                    "flex w-full items-start gap-2.5 px-3 py-2.5 text-left hover:bg-accent/40",
                    selected?.id === it.id && "bg-accent/60",
                  )}
                >
                  <Led tone={meta.tone} className="mt-1.5" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="eyebrow">{meta.label}</span>
                      <span className="text-[11px] text-muted-foreground">
                        <RelativeTime date={it.at} />
                      </span>
                    </div>
                    <p className="mt-0.5 truncate text-sm">{itemTitle(it)}</p>
                    <p className="truncate text-xs text-muted-foreground">{itemSubtitle(it)}</p>
                  </div>
                </button>
              </li>
            )
          })}
        </ul>
      </Panel>

      <Panel className="overflow-hidden">
        {selected ? (
          <ReviewDetail item={selected} onRequeue={onRequeue} />
        ) : (
          <EmptyState title="Bandeja al día" hint="No hay ítems pendientes de revisión. 🎉" />
        )}
      </Panel>
    </div>
  )
}

function itemTitle(it: ReviewItem): string {
  if (it.deadLetter) return `${it.deadLetter.stage} · inbox #${it.deadLetter.inboxId}`
  if (it.conflict) return `${it.conflict.a.title} ↔ ${it.conflict.b.title}`
  return ""
}
function itemSubtitle(it: ReviewItem): string {
  if (it.deadLetter) return it.deadLetter.lastError ?? ""
  if (it.conflict) return it.conflict.reason
  return ""
}

function ReviewDetail({
  item,
  onRequeue,
}: {
  item: ReviewItem
  onRequeue: (dl: WorkItemFailure) => void
}) {
  if (item.deadLetter) return <DeadLetterDetail dl={item.deadLetter} onRequeue={onRequeue} />
  if (item.conflict) return <ConflictDetail c={item.conflict} />
  return null
}

function DetailHeader({
  icon: Icon,
  eyebrow,
  title,
}: {
  icon: typeof Bug
  eyebrow: string
  title: string
}) {
  return (
    <div className="flex items-center gap-3 border-b border-border px-4 py-3">
      <div className="flex size-8 items-center justify-center rounded-md border border-border bg-muted/40">
        <Icon className="size-4 text-muted-foreground" />
      </div>
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h3 className="text-sm font-semibold">{title}</h3>
      </div>
    </div>
  )
}

function DeadLetterDetail({
  dl,
  onRequeue,
}: {
  dl: WorkItemFailure
  onRequeue: (dl: WorkItemFailure) => void
}) {
  return (
    <div className="flex h-full flex-col">
      <DetailHeader icon={Bug} eyebrow={`dead-letter · ${dl.stage}`} title={`inbox #${dl.inboxId}`} />
      <div className="space-y-4 overflow-y-auto p-4">
        <div className="flex flex-wrap gap-2 text-xs">
          <Chip label="Etapa" value={dl.stage} />
          <Chip label="Intentos" value={`${dl.attempts} / 3`} tone="error" />
          <Chip label="Actualizado" node={<RelativeTime date={dl.updatedAt} />} />
        </div>
        <Field label="Último error">
          <pre className="overflow-x-auto rounded-md border border-status-error/30 bg-status-error/5 p-3 font-mono text-[11px] text-status-error">
            {dl.lastError ?? "(sin detalle)"}
          </pre>
        </Field>
        <Field label="Mensaje original">
          <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">
            <p className="whitespace-pre-wrap text-muted-foreground">{dl.preview || "(sin texto)"}</p>
          </div>
        </Field>
        <p className="text-[11px] text-muted-foreground">
          El 402/saldo no llega aquí (aborta la corrida). Esto son fallos recuperables que cruzaron el
          umbral de 3 intentos.
        </p>
      </div>
      <div className="mt-auto flex flex-wrap gap-2 border-t border-border p-3">
        <Button size="sm" onClick={() => onRequeue(dl)}>
          <RotateCcw className="size-3.5" /> Reencolar
        </Button>
      </div>
    </div>
  )
}

function ConflictDetail({ c }: { c: CalendarConflict }) {
  return (
    <div className="flex h-full flex-col">
      <DetailHeader
        icon={CalendarClock}
        eyebrow="conflicto de calendario"
        title="Dos eventos importantes que se solapan"
      />
      <div className="space-y-4 overflow-y-auto p-4">
        <p className="rounded-md border border-status-review/30 bg-status-review/5 px-3 py-2 text-xs text-status-review">
          {c.reason}. No es un duplicado — ambos importan; nunca se fusiona automáticamente.
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <EventCard ev={c.a} />
          <EventCard ev={c.b} />
        </div>
        <p className="text-[11px] text-muted-foreground">
          La resolución de conflictos se administra desde el módulo de calendario.
        </p>
      </div>
    </div>
  )
}

function EventCard({ ev }: { ev: ConsolidatedEventLite }) {
  return (
    <div className="rounded-lg border border-border bg-background/40 p-3">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="truncate text-sm font-medium">{ev.title}</span>
        {ev.protected && <span className="eyebrow text-brand">protegido</span>}
      </div>
      <dl className="num space-y-1 text-xs text-muted-foreground">
        <Row k="Fecha" v={formatDate(ev.startsOn)} />
        <Row k="Horario" v={timeLabel(ev.startTime, ev.endTime)} />
        {ev.location && <Row k="Lugar" v={ev.location} />}
        <Row k="Prioridad" v={`rank ${ev.priorityRank}`} />
      </dl>
    </div>
  )
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-muted-foreground/70">{k}</dt>
      <dd className="text-right text-foreground">{v}</dd>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="eyebrow mb-1.5">{label}</div>
      {children}
    </div>
  )
}

function Chip({
  label,
  value,
  node,
  tone,
}: {
  label: string
  value?: string
  node?: React.ReactNode
  tone?: "error"
}) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-border bg-muted/40 px-2 py-1">
      <span className="eyebrow">{label}</span>
      <span className={cn("num font-medium", tone === "error" && "text-status-error")}>
        {node ?? value}
      </span>
    </span>
  )
}
