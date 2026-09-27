// Ventana de un asunto (/asuntos/:id), pensada para uso diario: arriba, en qué está (el estado
// vigente como lede, editable) y qué sigue (agenda de lo pendiente con fecha); al lado, lo que hace
// falta tener a la mano (datos: código de clase, enlaces). Después las tareas, qué pasó (cronología),
// los archivos, con qué otros asuntos se relaciona y quiénes participan. Todo lo de inspección
// (evidencia con método y confianza, historial de estados, entidades nombradas, ids) vive detrás del
// interruptor «Inspección» (lib/inspect-mode), que se recuerda.
// Acciones: actualizar estado, cerrar/reabrir, renombrar, fusionar (eligiendo el otro por nombre),
// borrar (solo candidatos), adjuntar archivos, anotar/quitar datos, relacionar, quitar evidencia.

import { useRef, useState } from "react"
import { Link, Navigate, useNavigate, useParams } from "react-router-dom"
import { toast } from "sonner"
import {
  ArrowLeft,
  Check,
  ChevronDown,
  ChevronRight,
  Clock,
  Copy,
  Download,
  ExternalLink,
  FileText,
  GitMerge,
  BellOff,
  Compass,
  Link2,
  Loader2,
  Lock,
  LockOpen,
  MessageSquarePlus,
  MoreHorizontal,
  Paperclip,
  Pencil,
  Plus,
  Trash2,
  X,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import { formatDateOnly, formatDateTime } from "@/lib/format"
import { KIND_LABEL, kindColor } from "@/lib/graph-kind"
import { INBOX_KIND_ICON, KIND_LABELS, inboxRefLabel } from "@/lib/inbox-format"
import { useInspectMode } from "@/lib/inspect-mode"
import {
  agendaOf,
  datoShape,
  dateParts,
  documentosOf,
  formatBytes,
  groupHitosByMonth,
  hitosNewestFirst,
  hostOf,
  localIsoDate,
  relativeDay,
  splitElenco,
  tareasSummary,
} from "@/lib/asunto-story"
import {
  ASUNTO_KIND_LABEL,
  ASUNTO_KINDS,
  ASUNTO_METHOD_LABEL,
  ASUNTO_PRIORITY_HINT,
  ASUNTO_STATUS_LABEL,
  ASUNTO_STATUS_TONE,
  RELATION_OPTIONS,
  relationPhrase,
} from "@/lib/asuntos"
import { AsuntoPicker } from "@/components/features/asuntos/asunto-picker"
import { TareasList } from "@/components/features/tareas/tareas-list"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { StatusBadge } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { Textarea } from "@/components/ui/textarea"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  addAsuntoDato,
  anotarAsunto,
  closeAsunto,
  deleteAsunto,
  fetchAsuntoStory,
  fetchMediaBlobUrl,
  fetchTareas,
  mergeAsuntos,
  rejectAsuntoEvidence,
  rejectAsuntoRelation,
  relateAsuntos,
  removeAsuntoDato,
  renameAsunto,
  reopenAsunto,
  planAsunto,
  setAsuntoState,
  silenceAsunto,
  unsilenceAsunto,
  uploadDocumento,
  type Asunto,
  type AsuntoDato,
  type AsuntoElencoItem,
  type AsuntoEvidence,
  type AsuntoHito,
  type AsuntoKind,
  type AsuntoRelation,
  type AsuntoState,
  type AsuntoStory,
  type Plan,
  type TareasAlCerrar,
  type Tarea,
} from "@/data"

type Kinds = Record<number, string>

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

async function copyText(v: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(v)
    toast.success("Copiado")
  } catch {
    toast.error("No se pudo copiar")
  }
}

// ---- Piezas -------------------------------------------------------------------------------------------

/** Bloque de la zona de uso: título en frase (no eyebrow), contador en gris y una pista corta. */
function Block({
  id,
  title,
  count,
  hint,
  right,
  children,
  className,
}: {
  id?: string
  title: string
  count?: number | string
  hint?: string
  right?: React.ReactNode
  children: React.ReactNode
  className?: string
}) {
  return (
    <section id={id} className={cn("rounded-lg border border-border bg-card", className)}>
      <header className="flex items-start justify-between gap-3 px-4 pb-2 pt-3">
        <div className="min-w-0">
          <h2 className="text-[13px] font-semibold leading-tight text-foreground">
            {title}
            {count != null && count !== "" && (
              <span className="num ml-1.5 font-normal text-muted-foreground">{count}</span>
            )}
          </h2>
          {hint && <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p>}
        </div>
        {right && <div className="flex shrink-0 items-center gap-1">{right}</div>}
      </header>
      <div className="px-4 pb-4">{children}</div>
    </section>
  )
}

function IconBtn({
  title,
  onClick,
  href,
  disabled = false,
  tone = "muted",
  children,
}: {
  title: string
  onClick?: () => void
  href?: string
  disabled?: boolean
  tone?: "muted" | "ok" | "danger"
  children: React.ReactNode
}) {
  const cls = cn(
    "inline-flex size-7 items-center justify-center rounded-md border border-transparent text-muted-foreground transition-colors hover:border-border hover:bg-muted hover:text-foreground disabled:opacity-40",
    tone === "ok" && "hover:text-status-ok",
    tone === "danger" && "hover:text-status-error",
  )
  if (href) {
    return (
      <a href={href} target="_blank" rel="noreferrer" title={title} aria-label={title} className={cls}>
        {children}
      </a>
    )
  }
  return (
    <button type="button" onClick={onClick} disabled={disabled} title={title} aria-label={title} className={cls}>
      {children}
    </button>
  )
}

/** Enlace al mensaje de origen: en modo inspección con rótulo («chat #123»); si no, solo el icono. */
function SourceRef({ id, kinds, inspect }: { id: number; kinds: Kinds; inspect: boolean }) {
  const Icon = INBOX_KIND_ICON[kinds[id] ?? "unknown"] ?? INBOX_KIND_ICON.unknown
  const label = inboxRefLabel(id, kinds)
  return (
    <Link
      to={`/datos/${id}`}
      className="inline-flex items-center gap-1 text-[11px] text-origin-inbox hover:underline"
      title={`Ver el mensaje de origen (${label})`}
    >
      <Icon className="size-3" />
      {inspect && label}
    </Link>
  )
}

function Chip({ children, className, title }: { children: React.ReactNode; className?: string; title?: string }) {
  return (
    <span
      title={title}
      className={cn("inline-flex items-center rounded-full border border-border px-2 py-0.5 text-[11px] text-muted-foreground", className)}
    >
      {children}
    </span>
  )
}

function Disclosure({
  label,
  open,
  onToggle,
  title,
}: {
  label: string
  open: boolean
  onToggle: () => void
  title?: string
}) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      title={title}
      className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
    >
      {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
      {label}
    </button>
  )
}

// ---- Cabecera: en qué está --------------------------------------------------------------------------

function Cover({
  asunto,
  inspect,
  onInspect,
  onChanged,
}: {
  asunto: Asunto
  inspect: boolean
  onInspect: (v: boolean) => void
  onChanged: () => void
}) {
  const navigate = useNavigate()
  const closed = asunto.status === "cerrado"
  const silenced = asunto.silencedAt !== null
  const [plan, setPlan] = useState<Plan | null>(null)
  const [planning, setPlanning] = useState(false)
  const [tareasAlCerrar, setTareasAlCerrar] = useState<TareasAlCerrar>("dejar")
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState("")
  const [busy, setBusy] = useState(false)
  const [dialog, setDialog] = useState<"rename" | "merge" | "close" | "reopen" | "silence" | "plan" | null>(null)
  const [name, setName] = useState(asunto.name)
  const [kind, setKind] = useState<AsuntoKind>(asunto.kind)
  const [survivor, setSurvivor] = useState<Asunto | null>(null)
  const [note, setNote] = useState("")

  async function run(label: string, fn: () => Promise<unknown>, after?: () => void): Promise<void> {
    setBusy(true)
    try {
      await fn()
      toast.success(label)
      setDialog(null)
      setNote("")
      if (after) after()
      else onChanged()
    } catch (e) {
      toast.error(`${label}: ${errMsg(e)}`)
    } finally {
      setBusy(false)
    }
  }

  function startEdit(): void {
    setDraft(asunto.stateText)
    setEditing(true)
  }

  async function saveState(): Promise<void> {
    const text = draft.trim()
    if (!text) return
    await run("Estado guardado", () => setAsuntoState(asunto.id, text))
    setEditing(false)
  }

  async function remove(): Promise<void> {
    if (!window.confirm(`¿Borrar el candidato «${asunto.name}»? Se pierde su evidencia asociada.`)) return
    await run("Asunto borrado", () => deleteAsunto(asunto.id), () => navigate("/asuntos", { replace: true }))
  }

  return (
    <header className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Link
          to="/asuntos"
          className="inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-xs text-muted-foreground hover:bg-muted/60 hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" /> Asuntos
        </Link>
        <div className="flex items-center gap-3">
          <label
            className="inline-flex cursor-pointer items-center gap-2 text-xs text-muted-foreground"
            title="Muestra cómo se armó el asunto: evidencia con método y confianza, historial de estados, entidades e ids"
          >
            <Switch size="sm" checked={inspect} onCheckedChange={onInspect} aria-label="Modo inspección" />
            Inspección
          </label>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="sm" variant="outline" aria-label="Más acciones" disabled={busy}>
                <MoreHorizontal className="size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-60">
              {!closed && (
                <DropdownMenuItem onSelect={startEdit}>
                  <Pencil className="size-3.5" /> Actualizar el estado
                </DropdownMenuItem>
              )}
              <DropdownMenuItem
                onSelect={() => {
                  setName(asunto.name)
                  setKind(asunto.kind)
                  setDialog("rename")
                }}
              >
                <Pencil className="size-3.5" /> Renombrar o cambiar el tipo
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={() => {
                  setSurvivor(null)
                  setDialog("merge")
                }}
              >
                <GitMerge className="size-3.5" /> Fusionar en otro asunto…
              </DropdownMenuItem>
              <DropdownMenuItem
                onSelect={() => {
                  setPlan(null)
                  setDialog("plan")
                }}
              >
                <Compass className="size-3.5" /> Armar el plan…
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              {closed ? (
                <DropdownMenuItem onSelect={() => setDialog("reopen")}>
                  <LockOpen className="size-3.5" /> Reabrir
                </DropdownMenuItem>
              ) : (
                <DropdownMenuItem onSelect={() => setDialog("close")}>
                  <Lock className="size-3.5" /> Cerrar el asunto
                </DropdownMenuItem>
              )}
              {silenced ? (
                <DropdownMenuItem
                  onSelect={() =>
                    void run("Ya no está silenciado", () => unsilenceAsunto(asunto.id))
                  }
                >
                  <BellOff className="size-3.5" /> Dejar de silenciar
                </DropdownMenuItem>
              ) : (
                <DropdownMenuItem onSelect={() => setDialog("silence")}>
                  <BellOff className="size-3.5" /> Silenciar…
                </DropdownMenuItem>
              )}
              {asunto.status === "candidato" && (
                <DropdownMenuItem variant="destructive" onSelect={() => void remove()}>
                  <Trash2 className="size-3.5" /> Borrar candidato
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      <div>
        <div className="flex flex-wrap items-center gap-2">
          <Chip>{ASUNTO_KIND_LABEL[asunto.kind] ?? asunto.kind}</Chip>
          <StatusBadge
            tone={ASUNTO_STATUS_TONE[asunto.status] ?? "neutral"}
            label={ASUNTO_STATUS_LABEL[asunto.status] ?? asunto.status}
          />
          {asunto.priority === "alta" && (
            <Chip
              className="border-status-review/40 text-status-review"
              title={asunto.priorityReason || ASUNTO_PRIORITY_HINT.alta}
            >
              prioridad alta
            </Chip>
          )}
          {inspect && <span className="num text-[11px] text-muted-foreground/70">#{asunto.id}</span>}
        </div>
        <h1 className="mt-2 text-2xl font-semibold leading-tight tracking-tight text-foreground">{asunto.name}</h1>
        {asunto.description && (
          <p className="mt-1.5 max-w-3xl text-sm leading-relaxed text-muted-foreground">{asunto.description}</p>
        )}
      </div>

      <div className="border-l-2 border-brand pl-4">
        {editing ? (
          <div className="space-y-2">
            <Textarea
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="En qué está este asunto, en una o dos frases…"
              className="min-h-20 text-[15px]"
              disabled={busy}
            />
            <div className="flex items-center gap-2">
              <Button size="sm" disabled={busy || !draft.trim()} onClick={() => void saveState()}>
                <Check className="size-3.5" /> Guardar estado
              </Button>
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => setEditing(false)}>
                Cancelar
              </Button>
            </div>
          </div>
        ) : (
          <div className="group flex items-start gap-2">
            <p className={cn("max-w-3xl text-[15px] leading-relaxed", !asunto.stateText && "text-muted-foreground")}>
              {asunto.stateText || (closed ? "Cerrado sin estado final." : "Todavía sin estado. ¿En qué está?")}
            </p>
            {!closed && (
              <IconBtn title="Actualizar el estado" onClick={startEdit}>
                <Pencil className="size-3.5" />
              </IconBtn>
            )}
          </div>
        )}
        <p className="mt-1.5 text-xs text-muted-foreground">
          {silenced ? (
            <span className="inline-flex items-center gap-1">
              <BellOff className="size-3" /> silenciado: no vuelve solo, aunque siga llegando correo
            </span>
          ) : closed && asunto.closedAt ? (
            <>cerrado el {formatDateTime(asunto.closedAt)}</>
          ) : asunto.stateUpdatedAt ? (
            <>
              estado actualizado <RelativeTime date={asunto.stateUpdatedAt} />
            </>
          ) : (
            "sin actualizaciones de estado"
          )}
        </p>
      </div>

      {!closed && <ContarAlgo asuntoId={asunto.id} onChanged={onChanged} />}

      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        {asunto.firstSignalAt && <span>desde el {formatDateOnly(asunto.firstSignalAt.slice(0, 10))}</span>}
        {asunto.lastSignalAt && (
          <span>
            última señal <RelativeTime date={asunto.lastSignalAt} />
          </span>
        )}
        <span className="num">{asunto.evidenceCount} mensajes</span>
        {inspect && asunto.aliases.length > 0 && (
          <span className="truncate" title={asunto.aliases.join(" · ")}>
            también: {asunto.aliases.join(" · ")}
          </span>
        )}
      </p>

      <Dialog open={dialog === "rename"} onOpenChange={(o) => !o && setDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Renombrar o cambiar el tipo</DialogTitle>
            <DialogDescription>El nombre anterior queda como alias, así se sigue reconociendo.</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Nombre" />
            <Select value={kind} onValueChange={(v) => setKind(v as AsuntoKind)}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ASUNTO_KINDS.map((k) => (
                  <SelectItem key={k} value={k}>
                    {ASUNTO_KIND_LABEL[k]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <DialogFooter>
            <Button variant="ghost" disabled={busy} onClick={() => setDialog(null)}>
              Cancelar
            </Button>
            <Button
              disabled={busy || !name.trim()}
              onClick={() =>
                void run("Asunto renombrado", () =>
                  renameAsunto(asunto.id, name.trim(), kind !== asunto.kind ? kind : undefined),
                )
              }
            >
              Guardar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={dialog === "merge"} onOpenChange={(o) => !o && setDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Fusionar en otro asunto</DialogTitle>
            <DialogDescription>
              «{asunto.name}» pasa a ser parte del asunto que elijas: sus mensajes, hitos, tareas y
              relaciones se mueven allá y este enlace redirige al sobreviviente.
            </DialogDescription>
          </DialogHeader>
          <AsuntoPicker value={survivor} onChange={setSurvivor} excludeId={asunto.id} autoFocus />
          <DialogFooter>
            <Button variant="ghost" disabled={busy} onClick={() => setDialog(null)}>
              Cancelar
            </Button>
            <Button
              disabled={busy || !survivor}
              onClick={() =>
                survivor &&
                void run(
                  `Fusionado en «${survivor.name}»`,
                  () => mergeAsuntos(survivor.id, asunto.id),
                  () => navigate(`/asuntos/${survivor.id}`, { replace: true }),
                )
              }
            >
              <GitMerge className="size-3.5" /> Fusionar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={dialog === "plan"}
        onOpenChange={(o) => {
          if (!o && !planning) setDialog(null)
        }}
      >
        <DialogContent className="max-h-[80vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Armar el plan</DialogTitle>
            <DialogDescription>
              Busca en la web, solo en fuentes oficiales, y arma los pasos: qué hacer, dónde, qué
              llevar y cuánto cuesta. Tarda varios minutos. Los pasos quedan como un proyecto del
              asunto, así que después los ves en «Qué sigue».
            </DialogDescription>
          </DialogHeader>
          {plan === null ? (
            <>
              <Input
                autoFocus
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Algo que deba saber (opcional): «ya tengo la cédula nueva»"
                disabled={planning}
              />
              <DialogFooter>
                <Button variant="ghost" disabled={planning} onClick={() => setDialog(null)}>
                  Cancelar
                </Button>
                <Button
                  disabled={planning}
                  onClick={() => {
                    setPlanning(true)
                    void planAsunto(asunto.id, note.trim())
                      .then((p) => {
                        setPlan(p)
                        setNote("")
                        onChanged()
                      })
                      .catch((e: unknown) => toast.error(`Plan: ${errMsg(e)}`))
                      .finally(() => setPlanning(false))
                  }}
                >
                  {planning ? (
                    <Loader2 className="size-3.5 animate-spin" />
                  ) : (
                    <Compass className="size-3.5" />
                  )}
                  {planning ? "Buscando en fuentes oficiales…" : "Armar el plan"}
                </Button>
              </DialogFooter>
            </>
          ) : (
            <>
              {plan.resumen && <p className="text-sm">{plan.resumen}</p>}
              <ol className="space-y-3 text-sm">
                {plan.pasos.map((p, i) => (
                  <li key={i} className="border-l-2 border-muted pl-3">
                    <p className="font-medium">
                      {i + 1}. {p.titulo}
                      {p.fecha && (
                        <span className="ml-2 text-xs text-muted-foreground">
                          para el {p.fecha}
                        </span>
                      )}
                    </p>
                    {p.queHacer && <p className="text-muted-foreground">{p.queHacer}</p>}
                    {p.donde && (
                      <p className="text-xs text-muted-foreground">Dónde: {p.donde}</p>
                    )}
                    {p.porQue && <p className="text-xs">Por qué esta opción: {p.porQue}</p>}
                    {p.llevar.length > 0 && (
                      <p className="text-xs text-muted-foreground">
                        Llevar: {p.llevar.join(", ")}
                      </p>
                    )}
                    {p.costo && <p className="text-xs text-muted-foreground">Costo: {p.costo}</p>}
                    {p.fuente && (
                      <a
                        className="text-xs text-muted-foreground underline"
                        href={p.fuente}
                        target="_blank"
                        rel="noreferrer"
                      >
                        fuente
                      </a>
                    )}
                  </li>
                ))}
              </ol>
              {plan.sinConfirmar.length > 0 && (
                <div className="rounded-md border border-dashed p-3 text-sm">
                  <p className="font-medium">Esto no lo pude confirmar en una fuente oficial</p>
                  <ul className="mt-1 list-disc pl-4 text-muted-foreground">
                    {plan.sinConfirmar.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                </div>
              )}
              <DialogFooter>
                <p className="mr-auto text-xs text-muted-foreground">
                  {plan.creadas} paso(s) nuevo(s)
                  {plan.repetidas > 0 ? `, ${plan.repetidas} que ya estaban` : ""}
                </p>
                <Button onClick={() => setDialog(null)}>Listo</Button>
              </DialogFooter>
            </>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={dialog === "silence"} onOpenChange={(o) => !o && setDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Silenciar el asunto</DialogTitle>
            <DialogDescription>
              Queda cerrado y no vuelve solo, aunque siga llegando correo de este hilo. Lo nuevo se
              sigue guardando acá, así que la historia no se pierde y no nace un asunto duplicado.
              {asunto.tareasPendientes > 0
                ? ` Se cancelan sus ${asunto.tareasPendientes} pendiente(s), porque silenciar es no voy a hacer nada con esto.`
                : ""}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setDialog(null)}>
              Cancelar
            </Button>
            <Button
              disabled={busy}
              onClick={() => void run("Asunto silenciado", () => silenceAsunto(asunto.id))}
            >
              <BellOff className="size-3.5" /> Silenciar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={dialog === "close" || dialog === "reopen"} onOpenChange={(o) => !o && setDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{dialog === "close" ? "Cerrar el asunto" : "Reabrir el asunto"}</DialogTitle>
            <DialogDescription>
              {dialog === "close"
                ? "Un asunto cerrado deja de recibir mensajes nuevos y sale de la lista de abiertos. Se puede reabrir después."
                : "Vuelve a la lista de abiertos y el asignador le sigue colgando mensajes."}
            </DialogDescription>
          </DialogHeader>
          <Textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder={dialog === "close" ? "Cómo terminó (opcional)…" : "En qué está ahora (opcional)…"}
            className="min-h-16"
          />
          {dialog === "close" && asunto.tareasPendientes > 0 && (
            <div className="space-y-1.5 rounded-md border p-3">
              <p className="text-sm font-medium">
                Queda{asunto.tareasPendientes === 1 ? "" : "n"} {asunto.tareasPendientes}{" "}
                pendiente{asunto.tareasPendientes === 1 ? "" : "s"}. ¿Qué hago con{" "}
                {asunto.tareasPendientes === 1 ? "ella" : "ellas"}?
              </p>
              {(
                [
                  ["dejar", "Dejarlas como están", "Siguen pendientes y se ven atrasadas."],
                  ["completar", "Darlas por hechas", "El asunto terminó bien y ya las hiciste."],
                  ["cancelar", "Cancelarlas", "Ya no aplican: el evento pasó o el plazo venció."],
                ] as [TareasAlCerrar, string, string][]
              ).map(([value, label, hint]) => (
                <label key={value} className="flex items-start gap-2 text-sm">
                  <input
                    type="radio"
                    name="tareas-al-cerrar"
                    className="mt-1"
                    checked={tareasAlCerrar === value}
                    onChange={() => setTareasAlCerrar(value)}
                  />
                  <span>
                    {label} <span className="text-muted-foreground">· {hint}</span>
                  </span>
                </label>
              ))}
            </div>
          )}
          <DialogFooter>
            <Button variant="ghost" disabled={busy} onClick={() => setDialog(null)}>
              Cancelar
            </Button>
            {dialog === "close" ? (
              <Button
                disabled={busy}
                onClick={() =>
                  void run("Asunto cerrado", () =>
                    closeAsunto(asunto.id, note.trim(), tareasAlCerrar),
                  )
                }
              >
                <Lock className="size-3.5" /> Cerrar el asunto
              </Button>
            ) : (
              <Button disabled={busy} onClick={() => void run("Asunto reabierto", () => reopenAsunto(asunto.id, note.trim()))}>
                <LockOpen className="size-3.5" /> Reabrir
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </header>
  )
}

// ---- Qué sigue ---------------------------------------------------------------------------------------

function AgendaBlock({ tareas, loading, error }: { tareas: Tarea[]; loading: boolean; error: string | null }) {
  const today = localIsoDate()
  const agenda = agendaOf(tareas, today, 5)

  function scrollToTareas(): void {
    document.getElementById("tareas")?.scrollIntoView({ behavior: "smooth", block: "start" })
  }

  return (
    <Block title="Qué sigue" hint="Lo pendiente con fecha, de lo más próximo a lo más lejano.">
      {error ? (
        <p className="text-xs text-status-error">No se pudieron cargar las tareas: {error}</p>
      ) : loading && tareas.length === 0 ? (
        <Skeleton className="h-20 w-full" />
      ) : agenda.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Nada con fecha por delante.
          {agenda.undated > 0 && (
            <>
              {" "}
              {agenda.undated === 1 ? "Hay 1 pendiente sin fecha" : `Hay ${agenda.undated} pendientes sin fecha`}{" "}
              <button type="button" onClick={scrollToTareas} className="underline-offset-2 hover:underline">
                en las tareas
              </button>
              .
            </>
          )}
        </p>
      ) : (
        <ol className="divide-y divide-border/60">
          {agenda.items.map((it) => {
            const p = dateParts(it.date)
            return (
              <li key={`${it.kind}-${it.id}`} className="grid grid-cols-[3.25rem_minmax(0,1fr)] items-center gap-3 py-2">
                <div
                  className={cn("num leading-none", it.late ? "text-status-error" : "text-foreground")}
                  title={`${p.weekday} ${p.day} de ${p.month}${p.year !== new Date().getFullYear() ? ` de ${p.year}` : ""}`}
                >
                  <div className="text-[22px] font-semibold tracking-tight">{p.day}</div>
                  <div className="mt-0.5 text-[11px] text-muted-foreground">{p.month}</div>
                </div>
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium" title={it.title}>
                    {it.title}
                  </div>
                  <div className={cn("text-[11px]", it.late ? "text-status-error" : "text-muted-foreground")}>
                    {it.kind === "entrega" ? (it.late ? "entrega vencida" : "entrega del proyecto") : it.late ? "venció" : "vence"}{" "}
                    {relativeDay(it.date, today)}
                    {it.detail ? ` · ${it.detail}` : ""}
                  </div>
                </div>
              </li>
            )
          })}
        </ol>
      )}
      {(agenda.more > 0 || (agenda.items.length > 0 && agenda.undated > 0)) && (
        <p className="mt-2 text-[11px] text-muted-foreground">
          {agenda.more > 0 && `${agenda.more} más con fecha`}
          {agenda.more > 0 && agenda.undated > 0 && " · "}
          {agenda.undated > 0 && `${agenda.undated} sin fecha`}
          {" · "}
          <button type="button" onClick={scrollToTareas} className="underline-offset-2 hover:underline">
            ver todas las tareas
          </button>
        </p>
      )}
    </Block>
  )
}

// ---- A la mano: datos ---------------------------------------------------------------------------------

function DatosBlock({
  asuntoId,
  datos,
  kinds,
  inspect,
  onChanged,
}: {
  asuntoId: number
  datos: AsuntoDato[]
  kinds: Kinds
  inspect: boolean
  onChanged: () => void
}) {
  const [adding, setAdding] = useState(false)
  const [label, setLabel] = useState("")
  const [value, setValue] = useState("")
  const [busy, setBusy] = useState(false)

  async function add(): Promise<void> {
    if (!value.trim()) return
    setBusy(true)
    try {
      await addAsuntoDato(asuntoId, label.trim() || "dato", value.trim())
      toast.success("Dato guardado")
      setLabel("")
      setValue("")
      setAdding(false)
      onChanged()
    } catch (e) {
      toast.error(`Guardar dato: ${errMsg(e)}`)
    } finally {
      setBusy(false)
    }
  }

  async function remove(d: AsuntoDato): Promise<void> {
    setBusy(true)
    try {
      await removeAsuntoDato(asuntoId, d.value)
      toast.success("Dato quitado")
      onChanged()
    } catch (e) {
      toast.error(`Quitar dato: ${errMsg(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Block
      title="A la mano"
      count={datos.length || undefined}
      hint="Códigos, enlaces y datos que hacen falta para participar. Nunca contraseñas."
      right={
        <Button size="xs" variant="ghost" onClick={() => setAdding((v) => !v)} aria-expanded={adding}>
          <Plus className="size-3" /> Anotar
        </Button>
      }
    >
      {datos.length === 0 && !adding && (
        <p className="text-sm text-muted-foreground">
          Todavía nada. Salen solos de los mensajes (código de clase, enlace de acceso, salón) o se anotan acá.
        </p>
      )}
      {datos.length > 0 && (
        <dl className="divide-y divide-border/60">
          {datos.map((d) => {
            const shape = datoShape(d.value)
            return (
              <div
                key={d.value}
                className="group grid grid-cols-[minmax(6rem,10rem)_minmax(0,1fr)_auto] items-center gap-x-3 py-1.5"
              >
                <dt className="truncate text-xs text-muted-foreground" title={d.evidence || d.label}>
                  {d.label}
                </dt>
                <dd className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-sm">
                  {shape === "url" ? (
                    <a
                      href={d.value}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex min-w-0 items-center gap-1 truncate hover:underline"
                      title={d.value}
                    >
                      <ExternalLink className="size-3 shrink-0 text-muted-foreground" />
                      <span className="truncate">{hostOf(d.value)}</span>
                    </a>
                  ) : shape === "code" ? (
                    <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[13px] break-all">{d.value}</code>
                  ) : (
                    <span className="break-words">{d.value}</span>
                  )}
                  {inspect && d.inboxIds.slice(0, 2).map((id) => <SourceRef key={id} id={id} kinds={kinds} inspect />)}
                </dd>
                <dd className="flex items-center gap-0.5">
                  <IconBtn title="Copiar" onClick={() => void copyText(d.value)}>
                    <Copy className="size-3.5" />
                  </IconBtn>
                  {!inspect && d.inboxIds.length > 0 && (
                    <span className="inline-flex size-7 items-center justify-center">
                      <SourceRef id={d.inboxIds[0]} kinds={kinds} inspect={false} />
                    </span>
                  )}
                  <span className="opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                    <IconBtn title="Quitar este dato" tone="danger" disabled={busy} onClick={() => void remove(d)}>
                      <X className="size-3.5" />
                    </IconBtn>
                  </span>
                </dd>
              </div>
            )
          })}
        </dl>
      )}
      {adding && (
        <form
          className={cn("flex flex-wrap items-center gap-2", datos.length > 0 && "mt-3 border-t border-border pt-3")}
          onSubmit={(e) => {
            e.preventDefault()
            void add()
          }}
        >
          <Input
            autoFocus
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="Qué es (código de clase, salón…)"
            className="h-8 w-48 text-sm"
            aria-label="Qué es el dato"
          />
          <Input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="El dato"
            className="h-8 min-w-48 flex-1 text-sm"
            aria-label="El dato"
          />
          <Button size="sm" type="submit" disabled={busy || !value.trim()}>
            Guardar
          </Button>
          <Button size="sm" type="button" variant="ghost" disabled={busy} onClick={() => setAdding(false)}>
            Cancelar
          </Button>
        </form>
      )}
    </Block>
  )
}

// ---- Tareas -------------------------------------------------------------------------------------------

function TareasBlock({
  tareas,
  loading,
  error,
  reload,
}: {
  tareas: Tarea[]
  loading: boolean
  error: string | null
  reload: () => void
}) {
  const [showClosed, setShowClosed] = useState(false)
  const [showOverdue, setShowOverdue] = useState(false)
  const abiertas = tareas.filter((t) => t.status === "pendiente")
  const vencidas = tareas.filter((t) => t.status === "vencida")
  const cerradas = tareas.filter((t) => t.status === "completada" || t.status === "cancelada")

  return (
    <Block
      id="tareas"
      title="Tareas"
      count={tareas.length ? tareasSummary(tareas) : undefined}
      hint="Lo que hay que hacer por este asunto; salió de los mensajes o se anotó a mano. Un proyecto muestra sus pasos debajo."
      className="scroll-mt-4"
    >
      {error ? (
        <ErrorState detail={error} onRetry={reload} className="py-6" />
      ) : loading && tareas.length === 0 ? (
        <Skeleton className="h-16 w-full" />
      ) : tareas.length === 0 ? (
        <p className="text-sm text-muted-foreground">Sin tareas todavía.</p>
      ) : (
        <div className="space-y-3">
          <TareasList tareas={abiertas} context={tareas} onChanged={reload} emptyText="Nada pendiente." />
          {vencidas.length > 0 && (
            <section>
              <Disclosure
                label={`${vencidas.length} ${vencidas.length === 1 ? "vencida" : "vencidas"}`}
                open={showOverdue}
                onToggle={() => setShowOverdue((v) => !v)}
                title="Pasó su fecha (la clase o sesión ya ocurrió) y el sistema la cerró sola. Se puede reabrir."
              />
              {showOverdue && <TareasList tareas={vencidas} context={tareas} onChanged={reload} />}
            </section>
          )}
          {cerradas.length > 0 && (
            <section>
              <Disclosure
                label={`${cerradas.length} ${cerradas.length === 1 ? "cerrada" : "cerradas"}`}
                open={showClosed}
                onToggle={() => setShowClosed((v) => !v)}
                title="Hechas, canceladas o descartadas."
              />
              {showClosed && <TareasList tareas={cerradas} context={tareas} onChanged={reload} />}
            </section>
          )}
        </div>
      )}
    </Block>
  )
}

// ---- Contar algo: la palabra del dueño -----------------------------------------------------------------

//: Lo que no vino en ningún mensaje («me dijeron que el viernes puedo pasar», «ya pagué») entra por
//: acá. Queda como hito y, si dice cuándo va a hacer un paso o que algo ya está hecho, el sistema lo
//: aplica y dice qué cambió. Siempre visible: es la puerta de entrada, no un menú.
function ContarAlgo({ asuntoId, onChanged }: { asuntoId: number; onChanged: () => void }) {
  const [texto, setTexto] = useState("")
  const [busy, setBusy] = useState(false)
  const [ultimo, setUltimo] = useState<{ entendido: string; cambios: string[]; interpretada: boolean } | null>(null)

  async function enviar(): Promise<void> {
    const t = texto.trim()
    if (!t || busy) return
    setBusy(true)
    try {
      const r = await anotarAsunto(asuntoId, t)
      setTexto("")
      setUltimo({ entendido: r.entendido, cambios: r.cambios, interpretada: r.interpretada })
      toast.success("Anotado", {
        description: r.cambios.length > 0 ? r.cambios.join(" · ") : r.interpretada ? "Queda como hito." : "Guardado sin interpretar.",
      })
      onChanged()
    } catch (e) {
      toast.error(`Contar algo: ${errMsg(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-3xl rounded-lg border border-border bg-card/40 p-3">
      <p className="mb-2 flex items-center gap-1.5 text-sm font-medium">
        <MessageSquarePlus className="size-4 text-muted-foreground" /> Contar algo
      </p>
      <Textarea
        value={texto}
        onChange={(e) => setTexto(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void enviar()
        }}
        placeholder="Algo que pasó o que te dijeron: «me dijeron que el viernes en la tarde puedo pasar», «ya pagué»"
        className="min-h-16 text-sm"
        disabled={busy}
      />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={busy || !texto.trim()} onClick={() => void enviar()}>
          {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
          {busy ? "Anotando…" : "Anotar"}
        </Button>
        <span className="text-xs text-muted-foreground">
          Queda en «Qué pasó». Si dice cuándo vas a hacer un paso o que algo ya está hecho, se aplica.
        </span>
      </div>
      {ultimo && (
        <div className="mt-2 rounded-md bg-muted/50 px-3 py-2 text-xs">
          {ultimo.entendido && <p className="text-muted-foreground">Entendí: {ultimo.entendido}</p>}
          {ultimo.cambios.length > 0 ? (
            <ul className="mt-1 space-y-0.5">
              {ultimo.cambios.map((c) => (
                <li key={c} className="flex items-start gap-1.5">
                  <Check className="mt-0.5 size-3 shrink-0 text-status-ok" /> {c}
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-muted-foreground">
              {ultimo.interpretada ? "Sin cambios que aplicar: queda como hito." : "Guardado sin interpretar (no hubo intérprete disponible)."}
            </p>
          )}
        </div>
      )}
    </div>
  )
}

// ---- Qué pasó: cronología -------------------------------------------------------------------------------

const CRONOLOGIA_FOLD = 10

function CronologiaBlock({
  hitos,
  evidenceById,
  kinds,
  inspect,
}: {
  hitos: AsuntoHito[]
  evidenceById: Map<number, AsuntoEvidence>
  kinds: Kinds
  inspect: boolean
}) {
  const [all, setAll] = useState(false)
  const sorted = hitosNewestFirst(hitos)
  const shown = all ? sorted : sorted.slice(0, CRONOLOGIA_FOLD)
  const months = groupHitosByMonth(shown)
  const hidden = sorted.length - shown.length

  return (
    <Block title="Qué pasó" count={hitos.length || undefined} hint="Los hitos del asunto, del más reciente al más viejo. ≈ marca una fecha aproximada.">
      {hitos.length === 0 ? (
        <p className="text-sm text-muted-foreground">Todavía sin hitos.</p>
      ) : (
        <div className="space-y-4">
          {months.map((mo) => (
            <section key={mo.key}>
              <h3 className="mb-1 text-xs font-medium capitalize text-muted-foreground">{mo.label}</h3>
              <ol className="divide-y divide-border/60">
                {mo.items.map((h) => {
                  const ev = h.evidenceId != null ? evidenceById.get(h.evidenceId) : undefined
                  const text = h.text || ev?.subject || "(sin texto)"
                  const key = h.notaId != null ? `n${h.notaId}` : `e${h.evidenceId ?? "x"}`
                  return (
                    <li key={key} className="grid grid-cols-[4.25rem_minmax(0,1fr)] gap-3 py-1.5">
                      <span className="num pt-0.5 text-[11px] text-muted-foreground">
                        {h.date ? `${h.dateIsExact ? "" : "≈ "}${dateParts(h.date).day} ${dateParts(h.date).month}` : "—"}
                      </span>
                      <div className="min-w-0">
                        <p className="text-sm leading-snug">{text}</p>
                        <p className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-muted-foreground">
                          {ev?.sender && <span className="truncate">{ev.sender}</span>}
                          {h.inboxId != null ? (
                            <SourceRef id={h.inboxId} kinds={kinds} inspect={inspect} />
                          ) : (
                            <span className="inline-flex items-center gap-1" title="Lo contaste vos">
                              <MessageSquarePlus className="size-3" /> lo contaste vos
                            </span>
                          )}
                        </p>
                      </div>
                    </li>
                  )
                })}
              </ol>
            </section>
          ))}
          {(hidden > 0 || all) && sorted.length > CRONOLOGIA_FOLD && (
            <button
              type="button"
              onClick={() => setAll((v) => !v)}
              className="text-xs text-muted-foreground underline-offset-2 hover:underline"
            >
              {all ? "Mostrar menos" : `Mostrar ${hidden} más`}
            </button>
          )}
        </div>
      )}
    </Block>
  )
}

// ---- Archivos ------------------------------------------------------------------------------------------

/** Una fila de archivo: nombre real, tamaño, fecha, bajar (blob de MinIO vía /media/{id}) y ver lo
 * que memex leyó de él (/datos/:id). */
function ArchivoRow({ doc, kinds, inspect }: { doc: AsuntoEvidence; kinds: Kinds; inspect: boolean }) {
  const [busy, setBusy] = useState(false)
  const name = doc.filename || doc.subject || inboxRefLabel(doc.inboxId, kinds)

  async function download(): Promise<void> {
    if (doc.mediaId == null) return
    setBusy(true)
    try {
      const url = await fetchMediaBlobUrl(doc.mediaId, { download: true })
      const a = document.createElement("a")
      a.href = url
      a.download = name
      document.body.appendChild(a)
      a.click()
      a.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (e) {
      toast.error(`Bajar ${name}: ${errMsg(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <li className="group flex items-center gap-2 py-1.5 text-sm">
      <FileText className="size-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate" title={name}>
        {name}
      </span>
      {doc.sizeBytes != null && (
        <span className="num shrink-0 text-[11px] text-muted-foreground">{formatBytes(doc.sizeBytes)}</span>
      )}
      <span className="num shrink-0 text-[11px] text-muted-foreground">
        {doc.occurredAt ? formatDateOnly(doc.occurredAt.slice(0, 10)) : ""}
      </span>
      <span className="flex shrink-0 items-center gap-0.5">
        {doc.mediaId != null && (
          <IconBtn title={`Bajar ${name}`} onClick={() => void download()} disabled={busy}>
            <Download className="size-3.5" />
          </IconBtn>
        )}
        <Link
          to={`/datos/${doc.inboxId}`}
          className="inline-flex size-7 items-center justify-center rounded-md border border-transparent text-muted-foreground hover:border-border hover:bg-muted hover:text-foreground"
          title={inspect ? `Ver lo que memex leyó (${inboxRefLabel(doc.inboxId, kinds)})` : "Ver lo que memex leyó de este archivo"}
          aria-label="Ver lo que memex leyó de este archivo"
        >
          <FileText className="size-3.5" />
        </Link>
      </span>
    </li>
  )
}

function ArchivosBlock({
  asuntoId,
  evidence,
  kinds,
  inspect,
  onChanged,
}: {
  asuntoId: number
  evidence: AsuntoEvidence[]
  kinds: Kinds
  inspect: boolean
  onChanged: () => void
}) {
  const [uploading, setUploading] = useState(false)
  const fileInput = useRef<HTMLInputElement | null>(null)
  const docs = documentosOf(evidence)

  async function onFiles(files: FileList | null): Promise<void> {
    if (!files || files.length === 0) return
    setUploading(true)
    try {
      for (const f of Array.from(files)) {
        const r = await uploadDocumento(f, { asuntoId })
        if (r.duplicado) toast.info(`${f.name}: ya estaba; quedó colgado del asunto`)
        else if (r.error) toast.warning(`${f.name}: ${r.error}`)
        else toast.success(`${f.name}: ${r.leido ? "leído" : "guardado"}${r.items ? ` · ${r.items} datos` : ""}`)
      }
      onChanged()
    } catch (e) {
      toast.error(`Adjuntar: ${errMsg(e)}`)
    } finally {
      setUploading(false)
      if (fileInput.current) fileInput.current.value = ""
    }
  }

  return (
    <Block
      title="Archivos"
      count={docs.length || undefined}
      hint="Diapositivas, programas, PDF o fotos del asunto. Se leen y de ahí salen tareas y datos."
      right={
        <>
          <input
            ref={fileInput}
            type="file"
            multiple
            accept=".pdf,.zip,.png,.jpg,.jpeg,.webp"
            className="hidden"
            onChange={(e) => void onFiles(e.target.files)}
          />
          <Button size="xs" variant="ghost" disabled={uploading} onClick={() => fileInput.current?.click()}>
            <Paperclip className="size-3" /> {uploading ? "Subiendo…" : "Adjuntar"}
          </Button>
        </>
      }
    >
      {docs.length === 0 ? (
        <p className="text-sm text-muted-foreground">Sin archivos todavía.</p>
      ) : (
        <ul className="divide-y divide-border/60">
          {docs.map((d) => (
            <ArchivoRow key={d.id} doc={d} kinds={kinds} inspect={inspect} />
          ))}
        </ul>
      )}
    </Block>
  )
}

// ---- Relacionado con ------------------------------------------------------------------------------------

function RelacionesBlock({
  asuntoId,
  relations,
  inspect,
  onChanged,
}: {
  asuntoId: number
  relations: AsuntoRelation[]
  inspect: boolean
  onChanged: () => void
}) {
  const [adding, setAdding] = useState(false)
  const [other, setOther] = useState<Asunto | null>(null)
  const [optKey, setOptKey] = useState(RELATION_OPTIONS[0].key)
  const [busy, setBusy] = useState(false)

  async function add(): Promise<void> {
    const opt = RELATION_OPTIONS.find((o) => o.key === optKey)
    if (!other || !opt) return
    setBusy(true)
    try {
      const [src, dst] = opt.direction === "out" ? [asuntoId, other.id] : [other.id, asuntoId]
      const created = await relateAsuntos(src, dst, opt.type)
      toast.success(created ? "Relación creada" : "La relación ya existía")
      setOther(null)
      setAdding(false)
      onChanged()
    } catch (e) {
      toast.error(`Relacionar: ${errMsg(e)}`)
    } finally {
      setBusy(false)
    }
  }

  async function remove(r: AsuntoRelation): Promise<void> {
    setBusy(true)
    try {
      await rejectAsuntoRelation(r.id)
      toast.success("Relación quitada")
      onChanged()
    } catch (e) {
      toast.error(`Quitar relación: ${errMsg(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Block
      title="Relacionado con"
      count={relations.length || undefined}
      hint="Otros asuntos de los que este es parte, que requiere o que continúa."
      right={
        <Button size="xs" variant="ghost" onClick={() => setAdding((v) => !v)} aria-expanded={adding}>
          <Link2 className="size-3" /> Relacionar
        </Button>
      }
    >
      {relations.length === 0 && !adding && <p className="text-sm text-muted-foreground">Sin relaciones todavía.</p>}
      {relations.length > 0 && (
        <ul className="divide-y divide-border/60">
          {relations.map((r) => (
            <li key={r.id} className="group flex flex-wrap items-center gap-x-2 gap-y-1 py-1.5 text-sm">
              <span className="text-muted-foreground">{relationPhrase(r.relationType, r.direction)}</span>
              <Link to={`/asuntos/${r.otherId}`} className="font-medium hover:underline">
                {r.otherName}
              </Link>
              <StatusBadge
                tone={ASUNTO_STATUS_TONE[r.otherStatus] ?? "neutral"}
                label={ASUNTO_STATUS_LABEL[r.otherStatus] ?? r.otherStatus}
              />
              {inspect && (
                <span className="num text-[11px] text-muted-foreground">
                  {r.source} · {r.confidence.toFixed(2)}
                </span>
              )}
              <span className="ml-auto opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                <IconBtn title="Quitar esta relación" tone="danger" disabled={busy} onClick={() => void remove(r)}>
                  <X className="size-3.5" />
                </IconBtn>
              </span>
            </li>
          ))}
        </ul>
      )}
      {adding && (
        <div className={cn("space-y-2", relations.length > 0 && "mt-3 border-t border-border pt-3")}>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-muted-foreground">Este asunto</span>
            <Select value={optKey} onValueChange={setOptKey}>
              <SelectTrigger size="sm" className="w-40">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {RELATION_OPTIONS.map((o) => (
                  <SelectItem key={o.key} value={o.key}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <AsuntoPicker value={other} onChange={setOther} excludeId={asuntoId} autoFocus />
          <div className="flex items-center gap-2">
            <Button size="sm" disabled={busy || !other} onClick={() => void add()}>
              <Link2 className="size-3.5" /> Relacionar
            </Button>
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setAdding(false)}>
              Cancelar
            </Button>
          </div>
        </div>
      )}
    </Block>
  )
}

// ---- Quiénes -------------------------------------------------------------------------------------------

function ElencoChip({ a }: { a: AsuntoElencoItem }) {
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border bg-muted/30 px-2 py-0.5 text-xs"
      title={`${KIND_LABEL[a.kind] ?? a.kind} · ${a.messages} mensajes`}
    >
      <span className="inline-block size-2 rounded-full" style={{ background: kindColor(a.kind) }} />
      {a.label}
      {a.messages > 0 && <span className="num text-[10px] text-muted-foreground">×{a.messages}</span>}
    </span>
  )
}

function QuienesBlock({ elenco, inspect }: { elenco: AsuntoElencoItem[]; inspect: boolean }) {
  const { people, rest } = splitElenco(elenco)
  return (
    <Block
      title="Quiénes"
      count={people.length || undefined}
      hint="Personas y organizaciones que aparecen en este asunto, por cantidad de mensajes."
    >
      {people.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {elenco.length === 0 ? "Todavía nadie identificado." : "Sin personas ni organizaciones identificadas todavía."}
        </p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {people.map((a) => (
            <ElencoChip key={`${a.slug}#${a.id}`} a={a} />
          ))}
        </div>
      )}
      {inspect && rest.length > 0 && (
        <div className="mt-3 border-t border-border pt-3">
          <p className="mb-1.5 text-[11px] text-muted-foreground">También atados al asunto (eventos, canales, registros):</p>
          <div className="flex flex-wrap gap-1.5">
            {rest.map((a) => (
              <ElencoChip key={`${a.slug}#${a.id}`} a={a} />
            ))}
          </div>
        </div>
      )}
    </Block>
  )
}

// ---- Inspección ------------------------------------------------------------------------------------------

const MEDIUM_ORDER = ["email", "chat", "social", "notes", "documentos", "unknown"]

function EvidenciaTab({ evidence, kinds, onChanged }: { evidence: AsuntoEvidence[]; kinds: Kinds; onChanged: () => void }) {
  const [busyId, setBusyId] = useState<number | null>(null)
  const byMedium = new Map<string, AsuntoEvidence[]>()
  for (const e of evidence) {
    const k = e.kind ?? "unknown"
    byMedium.set(k, [...(byMedium.get(k) ?? []), e])
  }
  const groups = MEDIUM_ORDER.filter((k) => byMedium.has(k)).map((k) => ({
    kind: k,
    label: KIND_LABELS[k] ?? "otros",
    items: (byMedium.get(k) ?? []).slice().sort((a, b) => (b.occurredAt ?? "").localeCompare(a.occurredAt ?? "")),
  }))

  async function remove(e: AsuntoEvidence): Promise<void> {
    setBusyId(e.id)
    try {
      await rejectAsuntoEvidence(e.id)
      toast.success(`Evidencia quitada (mensaje #${e.inboxId})`)
      onChanged()
    } catch (err) {
      toast.error(`Quitar evidencia: ${errMsg(err)}`)
    } finally {
      setBusyId(null)
    }
  }

  if (evidence.length === 0) return <p className="text-sm text-muted-foreground">Sin evidencia activa.</p>
  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        Cada mensaje que sostiene el asunto, con el método que lo ató (llm, hilo, evento, manual…) y su
        confianza. «Quitar» lo rechaza como evidencia y no vuelve a colgarse.
      </p>
      {groups.map((g) => {
        const Icon = INBOX_KIND_ICON[g.kind] ?? INBOX_KIND_ICON.unknown
        return (
          <section key={g.kind}>
            <div className="eyebrow mb-1.5 flex items-center gap-1.5">
              <Icon className="size-3" /> {g.label} ({g.items.length})
            </div>
            <ul>
              {g.items.map((e) => (
                <li
                  key={e.id}
                  className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border/60 py-1.5 text-sm last:border-0"
                >
                  <span className="num shrink-0 text-[11px] text-muted-foreground">
                    {e.occurredAt ? formatDateTime(e.occurredAt) : "sin fecha"}
                  </span>
                  <span className="shrink-0 font-medium">{e.sender || "(sin remitente)"}</span>
                  <Link
                    to={`/datos/${e.inboxId}`}
                    className="min-w-0 flex-1 truncate text-muted-foreground hover:underline"
                    title={e.subject}
                  >
                    {e.subject || inboxRefLabel(e.inboxId, kinds)}
                  </Link>
                  <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                    {ASUNTO_METHOD_LABEL[e.method] ?? e.method}
                  </span>
                  <span className="num text-[11px] text-muted-foreground">{e.confidence.toFixed(2)}</span>
                  <SourceRef id={e.inboxId} kinds={kinds} inspect />
                  <button
                    type="button"
                    disabled={busyId != null}
                    onClick={() => void remove(e)}
                    className="rounded px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted"
                    title="Rechazar este mensaje como evidencia"
                  >
                    Quitar
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )
      })}
    </div>
  )
}

function HistorialTab({ states }: { states: AsuntoState[] }) {
  const history = states.slice().sort((a, b) => b.createdAt.localeCompare(a.createdAt))
  if (history.length === 0) return <p className="text-sm text-muted-foreground">Sin estados registrados.</p>
  return (
    <ol className="space-y-1.5 text-sm">
      {history.map((s) => (
        <li key={s.id} className="flex flex-wrap items-start gap-x-2 gap-y-1">
          <span className="num shrink-0 text-[11px] text-muted-foreground">{formatDateTime(s.createdAt)}</span>
          <span className="min-w-0 flex-1">{s.stateText || <span className="text-muted-foreground">(vacío)</span>}</span>
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            {s.source}
          </span>
          {s.closed && (
            <span className="rounded bg-status-error/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-status-error">
              cierre
            </span>
          )}
          {s.inboxId != null && (
            <Link to={`/datos/${s.inboxId}`} className="text-[11px] text-origin-inbox hover:underline">
              mensaje #{s.inboxId}
            </Link>
          )}
        </li>
      ))}
    </ol>
  )
}

function TecnicoTab({ asunto }: { asunto: Asunto }) {
  const rows: [string, React.ReactNode][] = [
    ["id", <span className="num">#{asunto.id}</span>],
    ["semilla", asunto.seed || "—"],
    [
      "abierto por",
      asunto.openedByInboxId != null ? (
        <Link to={`/datos/${asunto.openedByInboxId}`} className="text-origin-inbox hover:underline">
          mensaje #{asunto.openedByInboxId}
        </Link>
      ) : (
        "—"
      ),
    ],
    ["prioridad", `${asunto.priority}${asunto.prioritySource ? ` (${asunto.prioritySource})` : ""}${asunto.priorityReason ? ` · ${asunto.priorityReason}` : ""}`],
    ["fecha límite del asunto", asunto.dueOn ? formatDateOnly(asunto.dueOn) : "—"],
    ["primera señal", asunto.firstSignalAt ? formatDateTime(asunto.firstSignalAt) : "—"],
    ["última señal", asunto.lastSignalAt ? formatDateTime(asunto.lastSignalAt) : "—"],
    ["mensajes (evidencia activa)", <span className="num">{asunto.evidenceCount}</span>],
    ["creado", formatDateTime(asunto.createdAt)],
    ["actualizado", formatDateTime(asunto.updatedAt)],
    ["alias", asunto.aliases.length ? asunto.aliases.join(" · ") : "—"],
  ]
  return (
    <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-6 gap-y-1.5 text-sm">
      {rows.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-xs text-muted-foreground">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

function InspeccionPanel({
  asunto,
  evidence,
  states,
  kinds,
  onChanged,
}: {
  asunto: Asunto
  evidence: AsuntoEvidence[]
  states: AsuntoState[]
  kinds: Kinds
  onChanged: () => void
}) {
  return (
    <Panel>
      <PanelHeader
        eyebrow="inspección"
        title="Cómo se armó este asunto"
        sub="Evidencia con método y confianza, historial de estados, entidades nombradas y datos técnicos."
      />
      <PanelBody>
        <Tabs defaultValue="evidencia">
          <TabsList variant="line">
            <TabsTrigger value="evidencia">Evidencia ({evidence.length})</TabsTrigger>
            <TabsTrigger value="historial">Historial de estado ({states.length})</TabsTrigger>
            <TabsTrigger value="entidades">Entidades ({asunto.entities.length})</TabsTrigger>
            <TabsTrigger value="tecnico">Técnico</TabsTrigger>
          </TabsList>
          <TabsContent value="evidencia" className="pt-3">
            <EvidenciaTab evidence={evidence} kinds={kinds} onChanged={onChanged} />
          </TabsContent>
          <TabsContent value="historial" className="pt-3">
            <HistorialTab states={states} />
          </TabsContent>
          <TabsContent value="entidades" className="pt-3">
            {asunto.entities.length === 0 ? (
              <p className="text-sm text-muted-foreground">Sin entidades nombradas.</p>
            ) : (
              <>
                <p className="mb-2 text-xs text-muted-foreground">
                  Tal como las escribió el asignador; sirven para reconocer el asunto en mensajes nuevos.
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {asunto.entities.map((e) => (
                    <span key={e} className="rounded-full border px-2 py-0.5 text-xs">
                      {e}
                    </span>
                  ))}
                </div>
              </>
            )}
          </TabsContent>
          <TabsContent value="tecnico" className="pt-3">
            <TecnicoTab asunto={asunto} />
          </TabsContent>
        </Tabs>
      </PanelBody>
    </Panel>
  )
}

// ---- Página ------------------------------------------------------------------------------------------------

function StoryBody({ data, version, onChanged }: { data: AsuntoStory; version: number; onChanged: () => void }) {
  const { asunto, datos, hitos, evidence, states, relations, elenco } = data
  const [inspect, setInspect] = useInspectMode()
  const tareasState = useAsync<Tarea[]>(() => fetchTareas({ asuntoId: asunto.id, status: "all" }), [asunto.id, version])
  const tareas = tareasState.data ?? []
  const kinds: Kinds = {}
  const evidenceById = new Map<number, AsuntoEvidence>()
  for (const e of evidence) {
    if (e.kind) kinds[e.inboxId] = e.kind
    evidenceById.set(e.id, e)
  }

  return (
    <div className="space-y-6">
      <Cover asunto={asunto} inspect={inspect} onInspect={setInspect} onChanged={onChanged} />

      <div className="grid gap-5 lg:grid-cols-2">
        <AgendaBlock tareas={tareas} loading={tareasState.loading} error={tareasState.error} />
        <DatosBlock asuntoId={asunto.id} datos={datos} kinds={kinds} inspect={inspect} onChanged={onChanged} />
      </div>

      <div className="grid gap-5 xl:grid-cols-5">
        <div className="space-y-5 xl:col-span-3">
          <TareasBlock tareas={tareas} loading={tareasState.loading} error={tareasState.error} reload={tareasState.reload} />
          <CronologiaBlock hitos={hitos} evidenceById={evidenceById} kinds={kinds} inspect={inspect} />
        </div>
        <div className="space-y-5 xl:col-span-2">
          <ArchivosBlock asuntoId={asunto.id} evidence={evidence} kinds={kinds} inspect={inspect} onChanged={onChanged} />
          <RelacionesBlock asuntoId={asunto.id} relations={relations} inspect={inspect} onChanged={onChanged} />
          <QuienesBlock elenco={elenco} inspect={inspect} />
        </div>
      </div>

      {inspect ? (
        <InspeccionPanel asunto={asunto} evidence={evidence} states={states} kinds={kinds} onChanged={onChanged} />
      ) : (
        <p className="text-[11px] text-muted-foreground">
          La inspección (evidencia con método y confianza, historial de estados, entidades, ids) está apagada.{" "}
          <button type="button" onClick={() => setInspect(true)} className="underline-offset-2 hover:underline">
            Encender
          </button>
        </p>
      )}
    </div>
  )
}

export function AsuntoStoryPage() {
  const { id } = useParams<{ id: string }>()
  const asuntoId = Number(id)
  const [version, setVersion] = useState(0)
  const bump = (): void => setVersion((v) => v + 1)
  const { data, error, loading, reload } = useAsync(() => fetchAsuntoStory(asuntoId), [asuntoId, version])

  const back = (
    <Link
      to="/asuntos"
      className="inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 text-xs text-muted-foreground hover:bg-muted/60 hover:text-foreground"
    >
      <ArrowLeft className="size-3.5" /> Asuntos
    </Link>
  )

  if (error) {
    return (
      <div className="space-y-4">
        {back}
        <ErrorState detail={error} onRetry={reload} />
      </div>
    )
  }
  if (loading && !data) {
    return (
      <div className="space-y-4">
        {back}
        <Skeleton className="h-8 w-2/3" />
        <Skeleton className="h-16 w-full" />
        <div className="grid gap-5 lg:grid-cols-2">
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-40 w-full" />
        </div>
      </div>
    )
  }
  if (!data) return null
  // Un id fusionado resuelve al sobreviviente: llevar la URL al id real.
  if (data.asunto.id !== asuntoId) return <Navigate to={`/asuntos/${data.asunto.id}`} replace />
  const empty = data.hitos.length === 0 && data.evidence.length === 0 && data.states.length === 0
  return (
    <div className="space-y-5">
      <StoryBody data={data} version={version} onChanged={bump} />
      {empty && (
        <EmptyState
          icon={<Clock className="size-5" />}
          title="Asunto sin contenido todavía"
          hint="No tiene mensajes ni hitos. El asignador lo irá llenando; también se le pueden adjuntar archivos."
        />
      )}
    </div>
  )
}
