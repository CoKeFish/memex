import { useEffect, useMemo, useRef } from "react"
import { Link } from "react-router-dom"
import { CornerUpLeft, Forward, Layers } from "lucide-react"
import { cn } from "@/lib/utils"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { RelativeTime } from "@/components/common/time"
import { TierTag } from "@/components/common/tier-tag"
import { fetchInboxWindow } from "@/data"
import { useAsync } from "@/lib/use-async"
import { useAutoRefresh } from "@/state/auto-refresh"
import { dayLabel, initials, sourceMeta, summarizeRow, type RowSummary } from "@/lib/inbox-format"
import type { InboxRow, InboxWindow, Source } from "@/types/domain"

/**
 * Lote de procesamiento del mensaje (GET /inbox/{id}/window): los mensajes que se resumieron —
 * o se resumirían — JUNTOS, con el mensaje actual resaltado. Es la respuesta visual a "¿por qué
 * el resumen menciona cosas que no están en este mensaje?": el resumen es de la ventana entera.
 * Chats se renderizan como conversación (burbujas); correos como filas compactas.
 */
export function ProcessingWindow({ row, source }: { row: InboxRow; source?: Source }) {
  const { data } = useAsync<InboxWindow>(() => fetchInboxWindow(row.id), [row.id])

  // Panel silencioso: sin data aún (carga/error) o sin lote real no aporta nada — no ocupa lugar.
  if (!data || data.mode === "none" || data.members.length <= 1) return null
  const members = data.members
  const kind = summarizeRow(members[0]).kind
  const isChat = kind === "chat" || kind === "social"

  return (
    <Panel>
      <PanelHeader
        eyebrow="lote de procesamiento"
        title={
          data.mode === "summary"
            ? `Resumidos juntos · ${members.length} mensajes`
            : `Se procesarán juntos · ${members.length} mensajes`
        }
        sub={
          data.mode === "summary"
            ? "El resumen de la fase «Resumen» cubre TODOS estos mensajes (comparten ventana conversacional)."
            : "Ventana prospectiva: lo que armaría «Resumir su lote» hoy (corte por gap de 6 h o tope de 40)."
        }
        right={<Layers className="size-4 text-muted-foreground" />}
      />
      <PanelBody>
        {isChat ? (
          <Conversation members={members} currentId={row.id} source={source} />
        ) : (
          <EmailRows members={members} currentId={row.id} />
        )}
      </PanelBody>
    </Panel>
  )
}

const TIME_FMT = new Intl.DateTimeFormat("es", { hour: "2-digit", minute: "2-digit" })

function str(v: unknown): string {
  return typeof v === "string" ? v : ""
}

/** Scrollea el mensaje actual a la vista cuando el lote es largo (una vez por carga). */
function useScrollToCurrent(dep: unknown) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    ref.current?.scrollIntoView({ block: "nearest" })
  }, [dep])
  return ref
}

/** Conversación (telegram/social): canaleta de avatares + burbujas, agrupando mensajes
 * consecutivos del mismo remitente (≤5 min) y separando por día. */
function Conversation({
  members,
  currentId,
  source,
}: {
  members: InboxRow[]
  currentId: number
  source?: Source
}) {
  const { now } = useAutoRefresh()
  const meta = sourceMeta(source)
  const currentRef = useScrollToCurrent(members)

  const chatTitle = str((members[0].payload as unknown as Record<string, unknown>).chat_title)

  const entries = useMemo(() => {
    const out: { m: InboxRow; s: RowSummary; header: boolean; sep: string | null }[] = []
    let prevDay = ""
    let prevSender = ""
    let prevAt = 0
    for (const m of members) {
      const s = summarizeRow(m)
      const day = dayLabel(m.occurredAt, now)
      const at = new Date(m.occurredAt).getTime()
      // Cabecera de burbuja al cambiar de remitente/día o tras una pausa > 5 min; el mensaje
      // ACTUAL siempre la lleva (ahí vive el marcador «este mensaje», aun agrupado).
      const header =
        day !== prevDay || s.sender !== prevSender || at - prevAt > 5 * 60_000 || m.id === currentId
      out.push({ m, s, header, sep: day !== prevDay ? day : null })
      prevDay = day
      prevSender = s.sender
      prevAt = at
    }
    return out
  }, [members, now, currentId])

  return (
    <div className="space-y-0.5">
      {chatTitle && (
        <div className="eyebrow mb-2">
          {chatTitle} · conversación tal como la ve el resumen
        </div>
      )}
      <div className="max-h-[420px] space-y-0.5 overflow-y-auto pr-1">
        {entries.map(({ m, s, header, sep }) => {
          const p = m.payload as unknown as Record<string, unknown>
          const current = m.id === currentId
          const text = str(p.text)
          const caption = str(p.media_caption)
          const body = (
            <div
              className={cn(
                "flex gap-2.5 rounded-md px-2 py-1",
                header && "mt-1.5",
                current
                  ? "bg-brand/10 ring-1 ring-brand/40"
                  : "hover:bg-accent/40",
              )}
            >
              <div className="w-6 shrink-0 pt-0.5">
                {header && (
                  <div
                    className={cn(
                      "num grid size-6 place-items-center rounded-full bg-muted text-[9px] font-semibold",
                      meta.tone,
                    )}
                  >
                    {initials(s.sender)}
                  </div>
                )}
              </div>
              <div className="min-w-0 flex-1">
                {header && (
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <span className={cn("text-xs font-medium", meta.tone)}>{s.sender}</span>
                    <span className="num text-[10px] text-muted-foreground">
                      {TIME_FMT.format(new Date(m.occurredAt))}
                    </span>
                    {current && <span className="eyebrow text-brand">este mensaje</span>}
                  </div>
                )}
                {str(p.forwarded_from) && (
                  <div className="mt-0.5 flex items-center gap-1 text-[11px] text-muted-foreground">
                    <Forward className="size-3" /> reenviado de {str(p.forwarded_from)}
                  </div>
                )}
                {p.reply_to_message_id != null && (
                  <div className="mt-0.5 flex items-center gap-1 text-[11px] text-muted-foreground">
                    <CornerUpLeft className="size-3" /> en respuesta a otro mensaje
                  </div>
                )}
                <p className="whitespace-pre-wrap break-words text-sm text-foreground/90">
                  {text || caption || (s.hasMedia ? "" : "(sin texto)")}
                  {s.hasMedia && (
                    <span className="num ml-1 rounded bg-muted px-1 py-px text-[10px] text-muted-foreground">
                      [{s.mediaLabel}]
                    </span>
                  )}
                </p>
                {text && caption && (
                  <p className="text-xs italic text-muted-foreground">{caption}</p>
                )}
              </div>
            </div>
          )
          return (
            <div key={m.id} ref={current ? currentRef : undefined}>
              {sep && (
                <div className="flex items-center gap-2 px-2 pb-1 pt-2.5">
                  <span className="eyebrow">{sep}</span>
                  <div className="h-px flex-1 bg-border" />
                </div>
              )}
              {current ? body : <Link to={`/datos/${m.id}`}>{body}</Link>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

/** Correos del lote como filas compactas: la mezcla de remitentes a la vista (el caso "¿por qué
 * el resumen habla de Steam?" se responde acá de un vistazo). */
function EmailRows({ members, currentId }: { members: InboxRow[]; currentId: number }) {
  const currentRef = useScrollToCurrent(members)
  return (
    <div className="max-h-[420px] space-y-0.5 overflow-y-auto pr-1">
      {members.map((m) => {
        const s = summarizeRow(m)
        const current = m.id === currentId
        const rowEl = (
          <div
            className={cn(
              "flex items-center gap-2 rounded-md px-2 py-1.5",
              current ? "bg-brand/10 ring-1 ring-brand/40" : "hover:bg-accent/40",
            )}
          >
            <span className="num grid size-6 shrink-0 place-items-center rounded-full bg-muted text-[9px] font-semibold">
              {initials(s.sender)}
            </span>
            <span className="min-w-0 shrink-0 truncate text-xs font-medium">{s.sender}</span>
            <span className="min-w-0 flex-1 truncate text-sm text-foreground/85">{s.title}</span>
            {current && <span className="eyebrow shrink-0 text-brand">este mensaje</span>}
            {m.classification && <TierTag tier={m.classification.tier} />}
            <span className="num shrink-0 text-[10px] text-muted-foreground">
              <RelativeTime date={m.occurredAt} />
            </span>
          </div>
        )
        return (
          <div key={m.id} ref={current ? currentRef : undefined}>
            {current ? rowEl : <Link to={`/datos/${m.id}`}>{rowEl}</Link>}
          </div>
        )
      })}
    </div>
  )
}
