// Cronología (story) de un cúmulo: el ELENCO (miembros sin fecha) como chips + los SUCESOS fechados
// en un eje vertical, agrupados por mes y en orden cronológico. Mismo lenguaje visual que
// `journey-timeline` (rail + card por ítem); el marcador toma el color del TIPO de vértice.

import { Link } from "react-router-dom"
import type { TimelineActor, TimelineEvent } from "@/data/graph"
import { formatDateOnly, formatDateTime, monthKey, monthLongLabel } from "@/lib/format"
import { KIND_LABEL, kindColor } from "@/lib/graph-kind"
import { INBOX_KIND_ICON, inboxRefLabel } from "@/lib/inbox-format"

function fmtAt(e: TimelineEvent): string {
  if (e.precision === "datetime") return formatDateTime(e.at)
  const d = formatDateOnly(e.at) // `at` es "YYYY-MM-DD" cuando no es datetime
  return e.precision === "inferred" ? `≈ ${d}` : d
}

/** Link al mensaje de origen con etiqueta+icono por su MEDIO real (correo/chat/social). */
function InboxRef({ id, kinds }: { id: number; kinds: Record<number, string> }) {
  const Icon = INBOX_KIND_ICON[kinds[id] ?? "unknown"] ?? INBOX_KIND_ICON.unknown
  return (
    <Link
      to={`/datos/${id}`}
      className="inline-flex items-center gap-1 text-[11px] text-origin-inbox hover:underline"
    >
      <Icon className="size-3" /> {inboxRefLabel(id, kinds)}
    </Link>
  )
}

interface MonthGroup {
  key: string
  label: string
  items: TimelineEvent[]
}

function groupByMonth(events: TimelineEvent[]): MonthGroup[] {
  const groups: MonthGroup[] = []
  for (const e of events) {
    const key = monthKey(e.at)
    let g = groups.find((x) => x.key === key)
    if (!g) {
      const [y, m] = key.split("-").map(Number)
      g = { key, label: monthLongLabel(new Date(y, m - 1, 1)), items: [] }
      groups.push(g)
    }
    g.items.push(e)
  }
  return groups
}

export function ClusterTimeline({
  events,
  actors,
  inboxKinds,
}: {
  events: TimelineEvent[]
  actors: TimelineActor[]
  /** Medio (email|chat|social) por id de inbox — etiqueta e icono del link al mensaje de origen. */
  inboxKinds: Record<number, string>
}) {
  const months = groupByMonth(events)
  return (
    <div className="space-y-6">
      {actors.length > 0 && (
        <div>
          <div className="eyebrow mb-1.5">Participantes ({actors.length})</div>
          <div className="flex flex-wrap gap-1.5">
            {actors.map((a) => (
              <span
                key={`${a.slug}#${a.id}`}
                className="inline-flex items-center gap-1.5 rounded-full border bg-muted/30 px-2 py-0.5 text-xs"
                title={KIND_LABEL[a.kind] ?? a.kind}
              >
                <span
                  className="inline-block size-2 rounded-full"
                  style={{ background: kindColor(a.kind) }}
                />
                {a.label}
              </span>
            ))}
          </div>
        </div>
      )}

      {events.length === 0 ? (
        <p className="text-sm text-muted-foreground">Este cúmulo no tiene sucesos con fecha.</p>
      ) : (
        <div className="space-y-5">
          {months.map((mo) => (
            <div key={mo.key}>
              <div className="eyebrow mb-2 capitalize">{mo.label}</div>
              <ol className="space-y-3">
                {mo.items.map((e, idx) => (
                  <li key={`${e.slug}#${e.id}`} className="flex gap-4">
                    <div className="flex flex-col items-center pt-1.5">
                      <span
                        className="size-2.5 shrink-0 rounded-full ring-2 ring-background"
                        style={{ background: kindColor(e.kind) }}
                      />
                      {idx < mo.items.length - 1 && <span className="mt-1 w-px flex-1 bg-border" />}
                    </div>
                    <div className="flex-1 pb-1">
                      <div className="rounded-lg border border-border bg-card p-3">
                        <div className="flex items-start justify-between gap-2">
                          <h3 className="text-sm font-medium leading-tight">{e.label}</h3>
                          <span className="num shrink-0 text-[11px] text-muted-foreground">
                            {fmtAt(e)}
                          </span>
                        </div>
                        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
                          <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
                            <span
                              className="inline-block size-2 rounded-full"
                              style={{ background: kindColor(e.kind) }}
                            />
                            {KIND_LABEL[e.kind] ?? e.kind}
                          </span>
                          {e.sourceInboxIds.length > 0 && (
                            <InboxRef id={e.sourceInboxIds[0]} kinds={inboxKinds} />
                          )}
                        </div>
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
