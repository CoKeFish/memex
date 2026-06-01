import { useMemo, useState } from "react"
import { Bug, CalendarClock, CheckCircle2, Copy, RotateCcw, ShieldQuestion, Trash2 } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { EmptyState } from "@/components/common/data-state"
import { Panel } from "@/components/common/panel"
import { Led } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { formatDate } from "@/lib/format"
import { originLabel, originText, type Tone } from "@/lib/status"
import { renderPayload } from "@/lib/render-payload"
import { getInbox, getMessageJourney, getReviewItems } from "@/data"
import { ReprocessButton } from "@/components/features/message/reprocess-button"
import { reprocessStepsFor } from "@/components/features/message/reprocess-steps"
import type {
  CalendarConflict,
  CalendarDedupCandidate,
  ConsolidatedEventLite,
  ReviewItem,
  ReviewKind,
  WorkItemFailure,
} from "@/types/domain"

const inbox = getInbox()
const reviewSeed = getReviewItems()

const KIND_META: Record<ReviewKind, { label: string; icon: typeof Bug; tone: Tone }> = {
  "dead-letter": { label: "Dead-letter", icon: Bug, tone: "error" },
  conflict: { label: "Conflicto", icon: CalendarClock, tone: "review" },
  dedup: { label: "Dedup", icon: Copy, tone: "running" },
}

function timeLabel(start: string | null, end: string | null): string {
  if (!start) return "todo el día"
  return end ? `${start}–${end}` : start
}

export function ReviewQueue() {
  const [items, setItems] = useState<ReviewItem[]>(reviewSeed)
  const [tab, setTab] = useState<"all" | ReviewKind>("all")
  const [selectedId, setSelectedId] = useState<string | null>(reviewSeed[0]?.id ?? null)

  const counts = useMemo(() => {
    const c = { all: items.length, "dead-letter": 0, conflict: 0, dedup: 0 } as Record<string, number>
    for (const it of items) c[it.kind]++
    return c
  }, [items])

  const visible = tab === "all" ? items : items.filter((i) => i.kind === tab)
  const selected = items.find((i) => i.id === selectedId) ?? visible[0] ?? null

  function resolve(item: ReviewItem, message: string) {
    setItems((prev) => prev.filter((i) => i.id !== item.id))
    if (selectedId === item.id) setSelectedId(null)
    toast.success(message, {
      description: "Quitado de la cola de revisión.",
      action: {
        label: "Deshacer",
        onClick: () => setItems((prev) => [item, ...prev].sort((a, b) => new Date(b.at).getTime() - new Date(a.at).getTime())),
      },
    })
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(320px,1fr)_minmax(0,1.4fr)]">
      <Panel className="flex flex-col overflow-hidden">
        <div className="border-b border-border p-2">
          <Tabs value={tab} onValueChange={(v) => setTab(v as typeof tab)}>
            <TabsList className="grid w-full grid-cols-4">
              <TabsTrigger value="all" className="text-xs">Todos · {counts.all}</TabsTrigger>
              <TabsTrigger value="dead-letter" className="text-xs">DL · {counts["dead-letter"]}</TabsTrigger>
              <TabsTrigger value="conflict" className="text-xs">Confl. · {counts.conflict}</TabsTrigger>
              <TabsTrigger value="dedup" className="text-xs">Dedup · {counts.dedup}</TabsTrigger>
            </TabsList>
          </Tabs>
        </div>
        <ul className="max-h-[620px] flex-1 divide-y divide-border overflow-y-auto">
          {visible.length === 0 && <EmptyState title="Nada pendiente aquí" hint="Bandeja vacía para este filtro." />}
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
          <ReviewDetail item={selected} onResolve={resolve} />
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
  if (it.dedup) return it.dedup.a.title
  return ""
}
function itemSubtitle(it: ReviewItem): string {
  if (it.deadLetter) return it.deadLetter.lastError ?? ""
  if (it.conflict) return it.conflict.reason
  if (it.dedup) return `${it.dedup.reason} · score ${it.dedup.score ?? "—"}`
  return ""
}

function ReviewDetail({ item, onResolve }: { item: ReviewItem; onResolve: (i: ReviewItem, m: string) => void }) {
  if (item.deadLetter) return <DeadLetterDetail item={item} dl={item.deadLetter} onResolve={onResolve} />
  if (item.conflict) return <ConflictDetail item={item} c={item.conflict} onResolve={onResolve} />
  if (item.dedup) return <DedupDetail item={item} d={item.dedup} onResolve={onResolve} />
  return null
}

function DetailHeader({ icon: Icon, eyebrow, title }: { icon: typeof Bug; eyebrow: string; title: string }) {
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

function DeadLetterDetail({ item, dl, onResolve }: { item: ReviewItem; dl: WorkItemFailure; onResolve: (i: ReviewItem, m: string) => void }) {
  const row = inbox.find((r) => r.id === dl.inboxId)
  const rendered = row ? renderPayload(row.payload, row.ocrText ?? "") : null
  return (
    <div className="flex h-full flex-col">
      <DetailHeader icon={Bug} eyebrow={`dead-letter · ${dl.stage}`} title={`inbox #${dl.inboxId}`} />
      <div className="space-y-4 overflow-y-auto p-4">
        <div className="flex flex-wrap gap-2 text-xs">
          <Chip label="Etapa" value={dl.stage} />
          <Chip label="Intentos" value={`${dl.attempts} / 3`} tone="error" />
          <Chip label="Actualizado" value="" node={<RelativeTime date={dl.updatedAt} />} />
        </div>
        <Field label="Último error">
          <pre className="overflow-x-auto rounded-md border border-status-error/30 bg-status-error/5 p-3 font-mono text-[11px] text-status-error">
            {dl.lastError}
          </pre>
        </Field>
        <Field label="Payload original (render_payload)">
          {rendered ? (
            <div className="rounded-md border border-border bg-muted/30 p-3 text-xs">
              {rendered.sender && <div className="mb-1 font-medium">{rendered.sender}</div>}
              <p className="whitespace-pre-wrap text-muted-foreground">{rendered.body || "(sin texto)"}</p>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">Mensaje no disponible.</p>
          )}
        </Field>
        <p className="text-[11px] text-muted-foreground">
          El 402/saldo no llega aquí (aborta la corrida). Esto son fallos recuperables que cruzaron el umbral de 3 intentos.
        </p>
      </div>
      <div className="mt-auto flex flex-wrap gap-2 border-t border-border p-3">
        <Button size="sm" onClick={() => onResolve(item, "Mensaje reencolado")}>
          <RotateCcw className="size-3.5" /> Reencolar
        </Button>
        <ReprocessButton inboxId={dl.inboxId} steps={reprocessStepsFor(getMessageJourney(dl.inboxId))} />
        <Button size="sm" variant="outline" onClick={() => onResolve(item, "Marcado como descartado")}>
          <Trash2 className="size-3.5" /> Descartar
        </Button>
      </div>
    </div>
  )
}

function ConflictDetail({ item, c, onResolve }: { item: ReviewItem; c: CalendarConflict; onResolve: (i: ReviewItem, m: string) => void }) {
  return (
    <div className="flex h-full flex-col">
      <DetailHeader icon={CalendarClock} eyebrow="conflicto de calendario" title="Dos eventos importantes que se solapan" />
      <div className="space-y-4 overflow-y-auto p-4">
        <p className="rounded-md border border-status-review/30 bg-status-review/5 px-3 py-2 text-xs text-status-review">
          {c.reason}. No es un duplicado — ambos importan; nunca se fusiona automáticamente.
        </p>
        <div className="grid gap-3 sm:grid-cols-2">
          <EventCard ev={c.a} />
          <EventCard ev={c.b} />
        </div>
      </div>
      <div className="mt-auto flex flex-wrap gap-2 border-t border-border p-3">
        <Button size="sm" variant="outline" onClick={() => onResolve(item, `Se conservó "${c.a.title}"`)}>Conservar A</Button>
        <Button size="sm" variant="outline" onClick={() => onResolve(item, `Se conservó "${c.b.title}"`)}>Conservar B</Button>
        <Button size="sm" onClick={() => onResolve(item, "Conflicto marcado como resuelto")}>
          <CheckCircle2 className="size-3.5" /> Resuelto
        </Button>
      </div>
    </div>
  )
}

function DedupDetail({ item, d, onResolve }: { item: ReviewItem; d: CalendarDedupCandidate; onResolve: (i: ReviewItem, m: string) => void }) {
  return (
    <div className="flex h-full flex-col">
      <DetailHeader icon={Copy} eyebrow={`dedup · score ${d.score ?? "—"}`} title="¿Es el mismo evento?" />
      <div className="space-y-4 overflow-y-auto p-4">
        <p className="rounded-md border border-border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">{d.reason}</p>
        <div className="grid gap-3 sm:grid-cols-2">
          <RawEventCard ev={d.a} />
          <RawEventCard ev={d.b} />
        </div>
      </div>
      <div className="mt-auto flex flex-wrap gap-2 border-t border-border p-3">
        <Button size="sm" onClick={() => onResolve(item, "Marcados como el mismo evento")}>
          <CheckCircle2 className="size-3.5" /> Es el mismo
        </Button>
        <Button size="sm" variant="outline" onClick={() => onResolve(item, "Marcados como distintos")}>Son distintos</Button>
        <Button size="sm" variant="ghost" onClick={() => onResolve(item, "Reenviado a recheck LLM")}>
          <ShieldQuestion className="size-3.5" /> Recheck LLM
        </Button>
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

function RawEventCard({ ev }: { ev: CalendarDedupCandidate["a"] }) {
  return (
    <div className="rounded-lg border border-border bg-background/40 p-3">
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="truncate text-sm font-medium">{ev.title}</span>
        <span className={cn("eyebrow", originText[ev.origin])}>{originLabel[ev.origin]}</span>
      </div>
      <dl className="num space-y-1 text-xs text-muted-foreground">
        <Row k="Fecha" v={formatDate(ev.startsOn)} />
        <Row k="Hora" v={ev.startTime ?? "todo el día"} />
        {ev.location && <Row k="Lugar" v={ev.location} />}
        {ev.provider && <Row k="Proveedor" v={ev.provider} />}
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

function Chip({ label, value, node, tone }: { label: string; value: string; node?: React.ReactNode; tone?: "error" }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-md border border-border bg-muted/40 px-2 py-1">
      <span className="eyebrow">{label}</span>
      <span className={cn("num font-medium", tone === "error" && "text-status-error")}>{node ?? value}</span>
    </span>
  )
}
