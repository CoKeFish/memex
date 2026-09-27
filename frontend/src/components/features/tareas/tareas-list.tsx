// Lista de tareas con acciones de estado. La usan el panel del asunto (/asuntos/:id) y la página
// /tareas. Orden: el que trae la API (vencidas y próximas primero, las sin fecha al final).
//
// Orden (pedido del dueño, 2026-09-15): la lista se agrupa por CUÁNDO (atrasadas, hoy, esta
// semana, más adelante, sin fecha) o por ASUNTO, y se ordena por fecha o por prioridad. Un proyecto
// de más de tres pasos empieza PLEGADO —nueve pasos tapaban todo lo demás— y su fila dice cuántos
// van atrasados, que es el número que si no se escondía.
//
// Legibilidad (pedido del dueño, 2026-09-13): cada tarea ocupa una fila de dos líneas fijas —
// arriba el título con la fecha en una pastilla; abajo, en gris y chico, de dónde salió (mensajes,
// enlaces, asunto)— y las acciones son iconos a la derecha que se aclaran al pasar el mouse. Un
// proyecto es una fila cabecera con su avance y la ÚNICA fecha (entrega); sus pasos cuelgan con una
// línea a la izquierda como lista de chequeo, sin fecha a la vista (el día en que se pidió cada uno
// queda en el tooltip): el dueño pidió consistencia, no un vencimiento por paso.

import React, { useState } from "react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import {
  Ban,
  Check,
  ChevronDown,
  ChevronRight,
  FolderKanban,
  Link2,
  MessageSquare,
  RotateCcw,
  X,
} from "lucide-react"
import { StatusBadge } from "@/components/common/led"
import { discardTarea, groupTareas, setTareaStatus, type Tarea, type TareaStatus } from "@/data"
import {
  agruparTareas,
  type TareasGroupBy,
  type TareasSortBy,
} from "@/lib/tareas-group"
import { ApiError } from "@/lib/api"
import { useInspectMode } from "@/lib/inspect-mode"
import {
  TAREA_CATEGORY_HINT,
  TAREA_CATEGORY_LABEL,
  TAREA_STATUS_HINT,
  TAREA_STATUS_LABEL,
  TAREA_STATUS_TONE,
} from "@/lib/tareas"
import { cn } from "@/lib/utils"

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.message
  return e instanceof Error ? e.message : String(e)
}

const dayFmt = new Intl.DateTimeFormat("es-CO", { day: "numeric", month: "short" })
const dayYearFmt = new Intl.DateTimeFormat("es-CO", { day: "numeric", month: "short", year: "numeric" })

/** «17 sep» (con año solo si no es el actual). */
function shortDay(iso: string): string {
  const d = new Date(`${iso}T12:00:00`)
  const fmt = d.getFullYear() === new Date().getFullYear() ? dayFmt : dayYearFmt
  return fmt.format(d).replace(/\./g, "")
}

function uniq(xs: string[]): string[] {
  return Array.from(new Set(xs))
}

//: Cuántos enlaces se ven antes del «+N». Tres alcanzan para reconocer de qué va la tarea; más
//: enlaces en una fila de dos líneas es justo lo que la vuelve ilegible.
const MAX_LINKS = 3
//: Hasta cuántos pasos un proyecto se muestra abierto de entrada. Con más, empieza plegado y se
//: abre desde su propia fila (pastilla «n de m pasos» con flecha, o el título).
const MAX_STEPS_ABIERTOS = 3

/**
 * Un enlace por SITIO, no por URL. Se agrupa por lo que se lee: cinco URLs distintas del mismo
 * dominio se ven como cinco veces la misma palabra, que es justo el ruido que se vino a quitar.
 * El `href` conserva la URL original (en un correo corporativo el envoltorio es el camino bueno).
 */
function linksVisibles(urls: string[]): { url: string; label: string }[] {
  const porSitio = new Map<string, string>()
  for (const u of urls) {
    const label = hostLabel(u)
    if (!porSitio.has(label)) porSitio.set(label, u)
  }
  return [...porSitio.entries()].map(([label, url]) => ({ url, label }))
}

/**
 * La URL de verdad detrás de un envoltorio de Outlook («safelinks»), que mete el destino en el
 * parámetro `url`. Sin esto, todos los enlaces de un correo de la universidad se ven iguales.
 */
function unwrapSafelink(url: string): string {
  try {
    const u = new URL(url)
    if (!u.hostname.endsWith("safelinks.protection.outlook.com")) return url
    return u.searchParams.get("url") ?? url
  } catch {
    return url
  }
}

function hostLabel(url: string): string {
  try {
    return new URL(unwrapSafelink(url)).hostname.replace(/^www\./, "")
  } catch {
    return url
  }
}

function IconAction({
  title,
  icon,
  disabled,
  onClick,
  tone = "muted",
}: {
  title: string
  icon: React.ReactNode
  disabled: boolean
  onClick: () => void
  tone?: "muted" | "ok" | "danger"
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      title={title}
      aria-label={title}
      className={cn(
        "inline-flex size-7 items-center justify-center rounded-md border border-transparent text-muted-foreground transition-colors hover:border-border hover:bg-muted disabled:opacity-40",
        tone === "ok" && "hover:text-status-ok",
        tone === "danger" && "hover:text-status-error",
      )}
    >
      {icon}
    </button>
  )
}

function DuePill({ t }: { t: Tarea }) {
  if (!t.dueOn) return <span className="text-[11px] text-muted-foreground/60">sin fecha</span>
  const closed = t.status === "completada" || t.status === "cancelada"
  const past = t.status === "vencida" || t.overdue
  const hint =
    t.dueSource === "proxima_sesion"
      ? "Para la siguiente clase del curso (fecha puesta por el sistema con el horario)"
      : "Fecha límite"
  return (
    <span
      className={cn(
        "num inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[11px]",
        past && !closed ? "bg-status-error/10 text-status-error" : "bg-muted text-muted-foreground",
      )}
      title={hint}
    >
      {past ? "venció" : "vence"} {shortDay(t.dueOn)}
    </span>
  )
}

/** Pasos vivos con la fecha ya pasada: el proyecto va atrasado aunque su entrega sea lejana. */
function lateSteps(steps: Tarea[]): number {
  const hoy = new Date().toISOString().slice(0, 10)
  return steps.filter((s) => s.status === "pendiente" && s.dueOn !== null && s.dueOn < hoy).length
}

/** Fecha de entrega de un proyecto: la mayor entre la suya y las de sus pasos vivos. */
function projectDueOf(t: Tarea, steps: Tarea[]): string | null {
  const dues = [t.dueOn, ...steps.filter((s) => s.status !== "cancelada").map((s) => s.dueOn)]
  const known = dues.filter((d): d is string => !!d)
  return known.length ? known.reduce((a, b) => (a > b ? a : b)) : null
}

function ProjectDuePill({ due }: { due: string | null }) {
  if (!due) return <span className="text-[11px] text-muted-foreground/60">sin fecha de entrega</span>
  const past = due < new Date().toISOString().slice(0, 10)
  return (
    <span
      className={cn(
        "num inline-flex shrink-0 items-center rounded-full px-2 py-0.5 text-[11px]",
        past ? "bg-status-error/10 text-status-error" : "bg-muted text-muted-foreground",
      )}
      title="Entrega del proyecto: la fecha mayor entre sus pasos"
    >
      entrega {shortDay(due)}
    </span>
  )
}

export function TareasList({
  tareas,
  context,
  onChanged,
  showAsunto = false,
  emptyText = "Sin tareas.",
  groupBy = "nada",
  sortBy = "fecha",
}: {
  tareas: Tarea[]
  /** Conjunto completo para resolver los pasos de cada proyecto (default: `tareas`). */
  context?: Tarea[]
  onChanged: () => void
  showAsunto?: boolean
  emptyText?: string
  /** Separadores de grupo. `nada` = una sola tira, como en el panel de un asunto. */
  groupBy?: TareasGroupBy
  sortBy?: TareasSortBy
}) {
  const [busyId, setBusyId] = useState<number | null>(null)
  // Proyectos desplegados a mano. Uno largo empieza plegado: nueve pasos tapaban todo lo demás.
  // El control para abrirlo va EN la fila del proyecto (la pastilla «n de m pasos» con flecha y el
  // título): el dueño no encontró el link chico de abajo (2026-09-17, «no sé cómo abrirlo»).
  const [abiertos, setAbiertos] = useState<Set<number>>(new Set())
  // Modo inspección (compartido con /asuntos): con él apagado los ids de los mensajes de origen
  // van al tooltip y cada origen es solo un icono con enlace.
  const [inspect] = useInspectMode()
  const { roots, childrenOf } = groupTareas(tareas, context)

  /** Hasta tres pasos se ven enteros; de ahí en adelante el proyecto empieza plegado. */
  const plegableDe = (steps: Tarea[]): boolean => steps.length > MAX_STEPS_ABIERTOS
  const abiertoDe = (t: Tarea, steps: Tarea[]): boolean => !plegableDe(steps) || abiertos.has(t.id)
  const toggle = (id: number): void =>
    setAbiertos((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  async function change(t: Tarea, status: TareaStatus, done: string): Promise<void> {
    setBusyId(t.id)
    try {
      await setTareaStatus(t.id, status)
      toast.success(done)
      onChanged()
    } catch (e) {
      toast.error(`Tarea #${t.id}: ${errMsg(e)}`)
    } finally {
      setBusyId(null)
    }
  }

  async function notATask(t: Tarea): Promise<void> {
    setBusyId(t.id)
    try {
      await discardTarea(t.id)
      toast.success("Descartada: no vuelve a crearse desde ese mensaje y quedó como feedback")
      onChanged()
    } catch (e) {
      toast.error(`Tarea #${t.id}: ${errMsg(e)}`)
    } finally {
      setBusyId(null)
    }
  }

  if (roots.length === 0) return <p className="text-sm text-muted-foreground">{emptyText}</p>

  function renderRow(t: Tarea, depth: number): React.ReactNode {
    const closed = t.status === "completada" || t.status === "cancelada"
    const busy = busyId === t.id
    const steps = childrenOf.get(t.id) ?? []
    const isProject = t.isParent || steps.length > 0
    const done = steps.filter((s) => s.status === "completada").length
    const sources = t.sourceInboxIds.slice(0, 3)
    const links = isProject ? uniq([...t.links, ...steps.flatMap((s) => s.links)]) : t.links
    const canOpen = t.status === "pendiente" || t.status === "vencida"
    const plegable = isProject && plegableDe(steps)
    const abierto = abiertoDe(t, steps)
    return (
      <li
        key={t.id}
        className={cn(
          "group grid grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-x-3 border-b border-border/60 py-2 last:border-0",
          closed && "opacity-60",
          t.status === "vencida" && "opacity-80",
          isProject && "bg-muted/20",
        )}
        data-depth={depth}
        title={
          `#${t.id} · ${TAREA_STATUS_HINT[t.status]}` +
          (depth > 0 && t.dueOn ? ` · pedido para el ${shortDay(t.dueOn)}` : "")
        }
      >
        <div className="flex items-center gap-1.5 pt-0.5">
          {isProject && <FolderKanban className="size-3.5 text-muted-foreground" />}
          <StatusBadge
            tone={TAREA_STATUS_TONE[t.status]}
            label={t.discardReason ? "Descartada" : TAREA_STATUS_LABEL[t.status]}
          />
        </div>

        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {plegable ? (
              <button
                type="button"
                onClick={() => toggle(t.id)}
                aria-expanded={abierto}
                className={cn(
                  "text-left text-sm font-medium leading-snug hover:underline",
                  isProject && "text-[15px]",
                  closed && "line-through decoration-border",
                )}
                title={abierto ? "Ocultar los pasos" : "Ver los pasos"}
              >
                {t.title}
              </button>
            ) : (
              <span
                className={cn(
                  "text-sm font-medium leading-snug",
                  isProject && "text-[15px]",
                  closed && "line-through decoration-border",
                )}
                title={t.description || undefined}
              >
                {t.title}
              </span>
            )}
            {isProject && plegable && (
              <button
                type="button"
                onClick={() => toggle(t.id)}
                aria-expanded={abierto}
                data-testid={`toggle-pasos-${t.id}`}
                className="num inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground hover:border-foreground/40 hover:text-foreground"
                title={
                  abierto
                    ? "Ocultar los pasos"
                    : "Ver los pasos · hechos / vivos (los descartados no cuentan)"
                }
              >
                {abierto ? (
                  <ChevronDown className="size-3" />
                ) : (
                  <ChevronRight className="size-3" />
                )}
                {`${done} de ${steps.length} pasos`}
              </button>
            )}
            {isProject && !plegable && (
              <span
                className="num rounded-full border px-2 py-0.5 text-[11px] text-muted-foreground"
                title="Pasos hechos / pasos vivos (los descartados no cuentan)"
              >
                {steps.length ? `${done} de ${steps.length} pasos` : "sin pasos"}
              </span>
            )}
            {isProject && lateSteps(steps) > 0 && (
              <span
                className="num rounded-full bg-status-error/10 px-2 py-0.5 text-[11px] text-status-error"
                title="Pasos cuya fecha ya pasó. La entrega del proyecto puede seguir lejos."
              >
                {lateSteps(steps)} atrasado{lateSteps(steps) === 1 ? "" : "s"}
              </span>
            )}
            {t.priority === "alta" && (
              <span className="rounded-full border border-status-error/40 px-1.5 text-[10px] text-status-error">
                prioridad alta
              </span>
            )}
            {t.category !== "otro" && (
              <span
                className={cn(
                  "rounded-full border px-1.5 text-[10px]",
                  t.category === "seguridad"
                    ? "border-status-error/60 bg-status-error/10 text-status-error"
                    : "border-border text-muted-foreground",
                )}
                title={TAREA_CATEGORY_HINT[t.category]}
              >
                {TAREA_CATEGORY_LABEL[t.category]}
              </span>
            )}
            {isProject ? (
              <ProjectDuePill due={projectDueOf(t, steps)} />
            ) : depth === 0 ? (
              <DuePill t={t} />
            ) : null}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
            {linksVisibles(links)
              .slice(0, MAX_LINKS)
              .map((l) => (
                <a
                  key={l.url}
                  href={l.url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 hover:underline"
                  title={unwrapSafelink(l.url)}
                >
                  <Link2 className="size-3" /> {l.label}
                </a>
              ))}
            {linksVisibles(links).length > MAX_LINKS && (
              <span
                className="num"
                title={linksVisibles(links)
                  .slice(MAX_LINKS)
                  .map((l) => l.label)
                  .join(", ")}
              >
                +{linksVisibles(links).length - MAX_LINKS}
              </span>
            )}
            {sources.length > 0 && inspect && (
              <span className="inline-flex items-center gap-1" title="Mensajes de los que salió">
                <MessageSquare className="size-3" />
                {sources.map((id, i) => (
                  <React.Fragment key={id}>
                    {i > 0 && <span className="text-muted-foreground/50">·</span>}
                    <Link to={`/datos/${id}`} className="num hover:underline">
                      {id}
                    </Link>
                  </React.Fragment>
                ))}
                {t.sourceInboxIds.length > 3 && (
                  <span className="text-muted-foreground/60">+{t.sourceInboxIds.length - 3}</span>
                )}
              </span>
            )}
            {sources.length > 0 && !inspect && (
              <span className="inline-flex items-center gap-0.5">
                {sources.map((id) => (
                  <Link
                    key={id}
                    to={`/datos/${id}`}
                    className="inline-flex items-center rounded p-0.5 hover:bg-muted hover:text-foreground"
                    title={`Ver el mensaje de origen (#${id})`}
                    aria-label={`Ver el mensaje de origen #${id}`}
                  >
                    <MessageSquare className="size-3" />
                  </Link>
                ))}
                {t.sourceInboxIds.length > 3 && (
                  <span className="text-muted-foreground/60">+{t.sourceInboxIds.length - 3}</span>
                )}
              </span>
            )}
            {showAsunto && t.asuntoId != null && (
              <Link to={`/asuntos/${t.asuntoId}`} className="truncate hover:underline">
                {t.asuntoName ?? `asunto #${t.asuntoId}`}
              </Link>
            )}
            {showAsunto && t.asuntoId == null && <span className="text-muted-foreground/60">sin asunto</span>}
          </div>
        </div>

        <div className="flex items-center gap-0.5 opacity-50 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
          {canOpen && (
            <IconAction
              title="Marcar como completada"
              icon={<Check className="size-3.5" />}
              disabled={busy}
              tone="ok"
              onClick={() => void change(t, "completada", "Tarea completada")}
            />
          )}
          {t.status !== "pendiente" && (
            <IconAction
              title="Reabrir (volver a pendiente)"
              icon={<RotateCcw className="size-3.5" />}
              disabled={busy}
              onClick={() => void change(t, "pendiente", "Tarea reabierta")}
            />
          )}
          {t.status === "pendiente" && (
            <IconAction
              title="Cancelar: no se va a hacer (sale del grafo, no se borra)"
              icon={<X className="size-3.5" />}
              disabled={busy}
              onClick={() => void change(t, "cancelada", "Tarea cancelada")}
            />
          )}
          {canOpen && t.origin === "extraction" && (
            <IconAction
              title="No es tarea: la extracción se equivocó. Se descarta, no vuelve a crearse desde ese mensaje y queda como feedback."
              icon={<Ban className="size-3.5" />}
              disabled={busy}
              tone="danger"
              onClick={() => void notATask(t)}
            />
          )}
        </div>
      </li>
    )
  }

  const pasosDe = (t: Tarea): Tarea[] => childrenOf.get(t.id) ?? []
  const grupos = agruparTareas(roots, pasosDe, { groupBy, sortBy })

  function renderTree(t: Tarea) {
    const steps = pasosDe(t)
    const plegable = plegableDe(steps)
    const abierto = abiertoDe(t, steps)
    return (
      <React.Fragment key={t.id}>
        {renderRow(t, 0)}
        {steps.length > 0 && abierto && (
          <li className="mb-1 ml-3 border-l-2 border-border/70 pl-3" data-steps-of={t.id}>
            <ul>{steps.map((c) => renderRow(c, 1))}</ul>
          </li>
        )}
        {plegable && abierto && (
          <li className="mb-1 ml-3 border-l-2 border-border/70 pl-3">
            <button
              type="button"
              className="py-1 text-xs text-muted-foreground underline-offset-2 hover:underline"
              onClick={() => toggle(t.id)}
            >
              Ocultar los pasos
            </button>
          </li>
        )}
      </React.Fragment>
    )
  }

  if (groupBy === "nada") {
    return (
      <ul data-testid="tareas-list" className="space-y-0">
        {grupos[0]?.tareas.map(renderTree)}
      </ul>
    )
  }

  // El separador lleva el conteo porque es lo que se quiere saber de un vistazo («el curso tiene
  // trece»). Solo lo atrasado se colorea: si todo grita, nada grita.
  return (
    <div data-testid="tareas-list" className="space-y-4">
      {grupos.map((g) => (
        <section key={g.key} data-group={g.key}>
          <h3
            className={cn(
              "mb-1 flex items-baseline gap-2 border-b pb-1 text-sm font-medium",
              g.urgent ? "border-status-error/30 text-status-error" : "text-foreground",
            )}
          >
            <span className="truncate">{g.label}</span>
            <span
              className={cn(
                "num text-xs font-normal",
                g.urgent ? "text-status-error/80" : "text-muted-foreground",
              )}
            >
              {g.tareas.length}
            </span>
          </h3>
          <ul className="space-y-0">{g.tareas.map(renderTree)}</ul>
        </section>
      ))}
    </div>
  )
}
