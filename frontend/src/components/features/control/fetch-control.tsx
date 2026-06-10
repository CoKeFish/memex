import { useEffect, useMemo, useRef, useState } from "react"
import { Link } from "react-router-dom"
import {
  ArrowRight,
  AtSign,
  ChevronDown,
  Download,
  FlaskConical,
  Loader2,
  Mail,
  Plus,
  Send,
  Trash2,
  TriangleAlert,
} from "lucide-react"
import type { LucideIcon } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { CollapsiblePanel } from "@/components/common/collapsible-panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { CapBadge, type CapLevel } from "@/components/common/cap-badge"
import { formatInt, formatRelative } from "@/lib/format"
import { sourceFullLabel, sourceMeta } from "@/lib/inbox-format"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import {
  fetchPullableSources,
  fetchSourceCheckpoint,
  fetchSources,
  ingestAdHoc,
  PAID_API_TYPES,
  triggerFetch,
} from "@/data"
import { addFollowedAccount, removeFollowedAccount } from "@/data/social"
import type { FetchPreview, Source } from "@/types/domain"

type Mode = "incremental" | "range" | "last"
const MODES: { v: Mode; label: string; cap: CapLevel }[] = [
  { v: "incremental", label: "Incremental (desde el último)", cap: "existe" },
  { v: "range", label: "Rango de fechas", cap: "existe" },
  { v: "last", label: "Últimos N", cap: "existe" },
]

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

// Qué modos admite cada tipo de fuente. Solo el correo (imap) honra la ventana de fechas/cantidad
// (rango / últimos N); telegram y redes solo traen lo nuevo (incremental) — su ingestor ignora esos
// parámetros, así que no los ofrecemos para no confundir.
function supportsMode(type: string, mode: Mode): boolean {
  return mode === "incremental" ? true : type === "imap"
}

function rec(v: unknown): Record<string, unknown> | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

type Cursor = Record<string, unknown> | null | undefined

/** Avance por cuenta seguida de un cursor social: handle → fecha (ISO) del último post traído. */
function socialAccountsProgress(cursor: Cursor): Record<string, string | null> {
  const accounts = rec(rec(cursor)?.accounts)
  if (!accounts) return {}
  const out: Record<string, string | null> = {}
  for (const [h, st] of Object.entries(accounts)) {
    const at = rec(st)?.last_posted_at
    out[h] = typeof at === "string" ? at : null
  }
  return out
}

/** Estado de la fuente en lenguaje claro a partir del cursor crudo (oculta llaves internas como
 * uidvalidity). Formas conocidas: imap → {folders:{INBOX:{last_uid,uidvalidity}}}; push (outlook)
 * → {last_received_at}; telegram → {chats:{id:{last_message_id}}}; redes → {accounts:{handle:…}}. */
function checkpointLabel(cursor: Cursor): string {
  if (!cursor) return "Aún no se ha traído nada de esta fuente."
  // imap: máximo last_uid entre las carpetas seguidas.
  const folders = rec(cursor.folders)
  if (folders) {
    const uids = Object.values(folders)
      .map((f) => Number(rec(f)?.last_uid))
      .filter((u) => Number.isFinite(u))
    if (uids.length) return `Último correo traído: #${Math.max(...uids)}.`
  }
  if (typeof cursor.last_uid === "number") return `Último correo traído: #${cursor.last_uid}.`
  if (typeof cursor.last_received_at === "string") {
    return `Último correo recibido: ${formatRelative(cursor.last_received_at)}.`
  }
  const chats = rec(cursor.chats)
  if (chats) {
    const n = Object.keys(chats).length
    return n
      ? `Avance guardado de ${n} ${n === 1 ? "chat" : "chats"}.`
      : "Aún no se ha traído nada de esta fuente."
  }
  const accounts = rec(cursor.accounts)
  if (accounts) {
    const n = Object.keys(accounts).length
    if (!n) return "Aún no se ha traído nada de esta fuente."
    const dates = Object.values(socialAccountsProgress(cursor))
      .filter((d): d is string => !!d)
      .sort()
    const latest = dates.at(-1)
    return latest
      ? `Avance de ${n} ${n === 1 ? "cuenta" : "cuentas"} · último post: ${formatRelative(latest)}.`
      : `Avance de ${n} ${n === 1 ? "cuenta" : "cuentas"}.`
  }
  return "Esta fuente ya tiene un punto guardado."
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="eyebrow mb-1 block">{label}</span>
      {children}
    </label>
  )
}

export function SourceSelect({
  sources,
  value,
  onChange,
}: {
  sources: Source[]
  value: string
  onChange: (v: string) => void
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="h-9 text-sm">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {sources.map((s) => (
          // El texto visible es "Proveedor · cuenta" (sourceFullLabel); el slug interno queda en el
          // tooltip por si hace falta para depurar.
          <SelectItem key={s.id} value={String(s.id)} className="text-sm" title={s.name}>
            {sourceFullLabel(s)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function PreviewView({ p, title, compact }: { p: FetchPreview; title?: string; compact?: boolean }) {
  const cells = [
    { label: "escaneados", value: p.scanned, cls: "text-foreground" },
    { label: "nuevos", value: p.nuevos, cls: "text-status-ok" },
    { label: "ya existentes", value: p.duplicados, cls: "text-muted-foreground" },
    { label: "filtrados", value: p.filtrados, cls: "text-status-filtered" },
  ]
  return (
    <div className="rounded-md border border-border bg-muted/30 p-3">
      {title && <div className="eyebrow mb-2">{title}</div>}
      <div className={cn("grid gap-2 text-center", compact ? "grid-cols-2" : "grid-cols-4")}>
        {cells.map((c) => (
          <div key={c.label}>
            <div className={cn("num text-lg font-semibold", c.cls)}>{formatInt(c.value)}</div>
            <div className="eyebrow mt-0.5">{c.label}</div>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[11px] text-muted-foreground">
        Los <span className="text-foreground">{formatInt(p.duplicados)} ya existentes</span> se
        ignoran: el sistema no guarda el mismo registro dos veces. No se insertan duplicados.
      </p>
    </div>
  )
}

type RowResult =
  | { status: "running" }
  | { status: "ok"; p: FetchPreview }
  | { status: "error"; msg: string }

/** Resultado compacto por fila tras correr el fetch sobre esa fuente / cuenta. */
function RowResultView({ r }: { r?: RowResult }) {
  if (!r) return null
  if (r.status === "running") return <Loader2 className="size-4 animate-spin text-muted-foreground" />
  if (r.status === "error") {
    return (
      <span className="text-xs text-status-error" title={r.msg}>
        error
      </span>
    )
  }
  const { p } = r
  return (
    <span className="num text-xs">
      <span className="text-status-ok">{formatInt(p.nuevos)} nuevos</span>
      <span className="text-muted-foreground"> · {formatInt(p.duplicados)} ya</span>
      {p.filtrados > 0 && <span className="text-status-filtered"> · {formatInt(p.filtrados)} filtr.</span>}
    </span>
  )
}

// ---- Agrupación: correo / telegram / una sección por red social -------------------------------

type GroupKey = "correo" | "telegram" | "x" | "instagram" | "facebook"

const GROUPS: { key: GroupKey; label: string; icon: LucideIcon; tone: string; paid: boolean }[] = [
  { key: "correo", label: "Correo", icon: Mail, tone: "text-chart-1", paid: false },
  { key: "telegram", label: "Telegram", icon: Send, tone: "text-chart-2", paid: false },
  { key: "x", label: "X (Twitter)", icon: AtSign, tone: "text-chart-4", paid: true },
  { key: "instagram", label: "Instagram", icon: AtSign, tone: "text-chart-4", paid: true },
  { key: "facebook", label: "Facebook", icon: AtSign, tone: "text-chart-4", paid: true },
]

function groupOf(s: Source): GroupKey {
  if (s.type === "imap") return "correo"
  if (s.type === "telegram") return "telegram"
  // fetchPullableSources garantiza instagram/facebook/x para el resto.
  return s.type as GroupKey
}

/** Nombre de la fila DENTRO de su grupo: el alias de la fuente, sin repetir el proveedor (que ya
 * es el título del grupo). Si no hay alias (p. ej. "Gmail"), el proveedor derivado. */
function rowLabel(s: Source): string {
  const m = sourceMeta(s)
  return m.account || m.label
}

/** Cuentas seguidas (allowlist) de una fuente social, desde `config.accounts`. */
function followedAccounts(s: Source): string[] {
  const arr = Array.isArray(s.config?.accounts) ? (s.config.accounts as unknown[]) : []
  return arr
    .map((a) => rec(a)?.account)
    .filter((x): x is string => typeof x === "string" && x.length > 0)
}

export function FetchControl() {
  const { data: sources, loading, error, reload } = useAsync<Source[]>(() => fetchPullableSources(), [])
  const [mode, setMode] = useState<Mode>("incremental")
  const [n, setN] = useState(50)
  const [since, setSince] = useState("")
  const [until, setUntil] = useState("")
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [results, setResults] = useState<Record<number, RowResult>>({})
  // Resultados de los fetch por-cuenta, keyed `${sourceId}:${handle}`.
  const [acctResults, setAcctResults] = useState<Record<string, RowResult>>({})
  const [busy, setBusy] = useState<null | "dry" | "run">(null)
  const [mutBusy, setMutBusy] = useState(false)
  const [ranReal, setRanReal] = useState(false)
  // Grupos expandidos. Todo colapsado al entrar: la vista queda corta aunque haya muchas fuentes.
  const [expanded, setExpanded] = useState<Set<GroupKey>>(new Set())
  // Borradores del input "seguir cuenta" por fuente social.
  const [drafts, setDrafts] = useState<Record<number, string>>({})
  // Bump para recargar los checkpoints después de una corrida real.
  const [ckptTick, setCkptTick] = useState(0)

  const modeMeta = MODES.find((m) => m.v === mode)!
  const allIds = useMemo(() => (sources ?? []).map((s) => s.id), [sources])
  // Fuentes que el modo actual puede traer (incremental = todas; rango/últimos N = solo correo).
  const enabledIds = useMemo(
    () => (sources ?? []).filter((s) => supportsMode(s.type, mode)).map((s) => s.id),
    [sources, mode],
  )

  // Punto guardado de cada fuente (línea bajo cada fila + avance por cuenta seguida).
  const { data: checkpoints } = useAsync<Record<number, Record<string, unknown> | null>>(async () => {
    const entries = await Promise.all(
      (sources ?? []).map(async (s) => {
        try {
          return [s.id, (await fetchSourceCheckpoint(s.id)).cursor] as const
        } catch {
          return [s.id, null] as const
        }
      }),
    )
    return Object.fromEntries(entries)
  }, [sources, ckptTick])

  // Selección inicial (una sola vez): correo + telegram. Las redes arrancan destildadas porque
  // cada corrida gasta API de paga — se tildan a mano o se trae una cuenta puntual.
  const initRef = useRef(false)
  useEffect(() => {
    if (!initRef.current && sources && sources.length) {
      setSelected(new Set(sources.filter((s) => !PAID_API_TYPES.has(s.type)).map((s) => s.id)))
      initRef.current = true
    }
  }, [sources])

  const allSelected = enabledIds.length > 0 && enabledIds.every((id) => selected.has(id))

  function toggle(id: number) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
    setRanReal(false)
  }
  function toggleAll() {
    setSelected(allSelected ? new Set() : new Set(enabledIds))
    setRanReal(false)
  }
  function toggleGroup(ids: number[], allSel: boolean) {
    setSelected((prev) => {
      const next = new Set(prev)
      for (const id of ids) {
        if (allSel) next.delete(id)
        else next.add(id)
      }
      return next
    })
    setRanReal(false)
  }
  function toggleExpand(key: GroupKey) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }
  function changeMode(nm: Mode) {
    setMode(nm)
    // Al pasar a rango/últimos N, destildá las fuentes que ese modo no admite.
    setSelected((prev) => new Set([...prev].filter((id) => {
      const s = (sources ?? []).find((x) => x.id === id)
      return s ? supportsMode(s.type, nm) : false
    })))
    setRanReal(false)
  }

  const agg = useMemo(() => {
    const a: FetchPreview = { scanned: 0, nuevos: 0, duplicados: 0, filtrados: 0 }
    for (const id of allIds) {
      const r = results[id]
      if (r?.status === "ok") {
        a.scanned += r.p.scanned
        a.nuevos += r.p.nuevos
        a.duplicados += r.p.duplicados
        a.filtrados += r.p.filtrados
      }
    }
    return a
  }, [results, allIds])
  const okCount = allIds.filter((id) => results[id]?.status === "ok").length

  async function run(dryRun: boolean) {
    const ids = allIds.filter((id) => selected.has(id))
    if (!ids.length) return
    setBusy(dryRun ? "dry" : "run")
    setResults((r) => {
      const nr = { ...r }
      for (const id of ids) nr[id] = { status: "running" }
      return nr
    })
    let ok = 0
    let err = 0
    const total: FetchPreview = { scanned: 0, nuevos: 0, duplicados: 0, filtrados: 0 }
    // Secuencial: una fuente a la vez para no saturar el server y mostrar avance fila por fila.
    for (const id of ids) {
      try {
        const res = await triggerFetch(id, {
          dryRun,
          mode,
          since: mode === "range" ? since || undefined : undefined,
          until: mode === "range" ? until || undefined : undefined,
          limit: mode === "last" ? n : undefined,
        })
        const p: FetchPreview = {
          scanned: res.posted,
          nuevos: res.inserted,
          duplicados: res.duplicates,
          filtrados: res.filtered,
        }
        setResults((r) => ({ ...r, [id]: { status: "ok", p } }))
        total.nuevos += p.nuevos
        total.duplicados += p.duplicados
        total.filtrados += p.filtrados
        ok++
      } catch (e) {
        setResults((r) => ({ ...r, [id]: { status: "error", msg: errMsg(e) } }))
        err++
      }
    }
    setBusy(null)
    if (!dryRun && ok > 0) {
      setRanReal(true)
      setCkptTick((t) => t + 1)
    }
    const verb = dryRun ? "Dry-run" : "Ingesta"
    const desc = `${total.nuevos} nuevos · ${total.duplicados} ya existentes · ${total.filtrados} filtrados`
    if (err === 0) {
      toast.success(`${verb}: ${ok} ${ok === 1 ? "fuente" : "fuentes"}`, { description: desc })
    } else {
      toast.warning(`${verb}: ${ok} ok · ${err} con error`, { description: desc })
    }
  }

  // Fetch incremental de UNA cuenta seguida: una corrida de Apify solo para ese handle. El cursor
  // social es por-cuenta, así que el avance de las demás cuentas de la fuente queda intacto.
  async function runAccount(s: Source, handle: string) {
    const key = `${s.id}:${handle}`
    setAcctResults((r) => ({ ...r, [key]: { status: "running" } }))
    try {
      const res = await triggerFetch(s.id, { accounts: [handle] })
      const p: FetchPreview = {
        scanned: res.posted,
        nuevos: res.inserted,
        duplicados: res.duplicates,
        filtrados: res.filtered,
      }
      setAcctResults((r) => ({ ...r, [key]: { status: "ok", p } }))
      setRanReal(true)
      setCkptTick((t) => t + 1)
      toast.success(`@${handle}: ${p.nuevos} ${p.nuevos === 1 ? "nuevo" : "nuevos"}`, {
        description: `${p.duplicados} ya existentes · ${p.filtrados} filtrados`,
      })
    } catch (e) {
      setAcctResults((r) => ({ ...r, [key]: { status: "error", msg: errMsg(e) } }))
      toast.error(`No se pudo traer @${handle}`, { description: errMsg(e) })
    }
  }

  async function addAccount(s: Source) {
    const raw = (drafts[s.id] ?? "").trim()
    if (!raw) return
    setMutBusy(true)
    try {
      await addFollowedAccount(s.id, raw)
      setDrafts((d) => ({ ...d, [s.id]: "" }))
      toast.success("Cuenta agregada", { description: "entra en la próxima corrida de esta fuente" })
      reload()
    } catch (e) {
      toast.error("No se pudo agregar", { description: errMsg(e) })
    } finally {
      setMutBusy(false)
    }
  }

  async function removeAccount(s: Source, handle: string) {
    setMutBusy(true)
    try {
      await removeFollowedAccount(s.id, handle)
      toast.success(`Se dejó de seguir @${handle}`)
      reload()
    } catch (e) {
      toast.error("No se pudo quitar", { description: errMsg(e) })
    } finally {
      setMutBusy(false)
    }
  }

  const missingRange = mode === "range" && !since
  const disabled = busy !== null || selected.size === 0 || missingRange
  // Atajo: si corrió una sola fuente, filtro por ella; si fueron varias, la vista completa.
  const ranIds = allIds.filter((id) => selected.has(id))
  const datosHref = ranIds.length === 1 ? `/datos?source=${ranIds[0]}` : "/datos"

  const groups = useMemo(
    () =>
      GROUPS.map((g) => ({ ...g, sources: (sources ?? []).filter((s) => groupOf(s) === g.key) }))
        .filter((g) => g.sources.length > 0),
    [sources],
  )

  return (
    <Panel>
      <PanelHeader
        eyebrow="ingesta · fetch"
        title="Traer a demanda"
        sub="Fuentes agrupadas por proveedor; dentro de cada red, cada fuente con sus cuentas seguidas. Tildá y traé en lote, o traé una cuenta puntual."
        right={<CapBadge level={modeMeta.cap} title="incremental trae solo lo nuevo y guarda el avance; rango y últimos N son descargas históricas que no afectan ese avance" />}
      />
      <PanelBody className="space-y-3">
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading ? (
          <div className="flex items-center gap-2 px-2 py-8 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Cargando fuentes…
          </div>
        ) : !sources || sources.length === 0 ? (
          <EmptyState
            title="No hay fuentes traíbles"
            hint="Creá una fuente (correo, Telegram o redes) para poder traer a demanda."
          />
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Modo">
                <Select value={mode} onValueChange={(v) => changeMode(v as Mode)}>
                  <SelectTrigger className="h-9 text-sm"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {MODES.map((m) => (
                      <SelectItem key={m.v} value={m.v} className="text-sm">{m.label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
              {mode === "last" && (
                <Field label="Cantidad">
                  <Input type="number" value={n} min={1} onChange={(e) => setN(Number(e.target.value))} className="h-9" />
                </Field>
              )}
              {mode === "range" && (
                <div className="grid grid-cols-2 gap-2">
                  <Field label="Desde"><Input type="date" value={since} onChange={(e) => setSince(e.target.value)} className="h-9" /></Field>
                  <Field label="Hasta"><Input type="date" value={until} onChange={(e) => setUntil(e.target.value)} className="h-9" /></Field>
                </div>
              )}
            </div>

            {mode === "incremental" && (
              <p className="text-xs text-muted-foreground">
                Trae solo lo nuevo desde la última vez y guarda el avance. Funciona para todas las
                fuentes (correo, Telegram, redes).
              </p>
            )}
            {mode === "range" && (
              <p className="text-xs text-muted-foreground">
                Descarga histórica del rango elegido (los más recientes primero), solo correo. No
                afecta el avance del modo incremental.
              </p>
            )}
            {mode === "last" && (
              <p className="text-xs text-muted-foreground">
                Descarga los <span className="num">{n}</span> mensajes más recientes por fuente
                (solo correo). No afecta el avance del modo incremental.
              </p>
            )}
            {missingRange && (
              <p className="text-xs text-status-review">Indicá al menos la fecha “Desde”.</p>
            )}

            {/* Acciones arriba (siempre visibles); la lista agrupada abajo. */}
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="outline" size="sm" disabled={disabled} onClick={() => run(true)}>
                {busy === "dry" ? <Loader2 className="size-3.5 animate-spin" /> : <FlaskConical className="size-3.5" />} Dry-run
              </Button>
              <Button size="sm" disabled={disabled} onClick={() => run(false)}>
                {busy === "run" ? <Loader2 className="size-3.5 animate-spin" /> : <Download className="size-3.5" />}
                {selected.size > 1 ? `Traer ${selected.size} ahora` : "Traer ahora"}
              </Button>
              {ranReal && (
                <Link
                  to={datosHref}
                  className="ml-1 inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline"
                >
                  Ver en datos <ArrowRight className="size-3.5" />
                </Link>
              )}
            </div>

            {okCount > 0 && (
              <div className="max-w-md">
                <PreviewView p={agg} compact title={okCount > 1 ? `total · ${okCount} fuentes` : undefined} />
              </div>
            )}

            <div className="divide-y divide-border overflow-hidden rounded-md border border-border">
              <div className="flex items-center gap-2.5 bg-muted/30 px-3 py-2">
                <Switch
                  checked={allSelected}
                  disabled={enabledIds.length === 0}
                  onCheckedChange={toggleAll}
                  aria-label="Seleccionar todas las fuentes"
                />
                <button
                  type="button"
                  onClick={toggleAll}
                  disabled={enabledIds.length === 0}
                  className="flex flex-1 items-center text-left disabled:cursor-default"
                >
                  <span className="text-sm font-medium">Todas las fuentes</span>
                  <span className="num ml-auto text-xs text-muted-foreground">
                    {selected.size}/{enabledIds.length}
                  </span>
                </button>
              </div>

              {groups.map((g) => {
                const ids = g.sources.map((s) => s.id)
                const groupEnabled = g.sources.filter((s) => supportsMode(s.type, mode)).map((s) => s.id)
                const selCount = ids.filter((id) => selected.has(id)).length
                const allSel = groupEnabled.length > 0 && groupEnabled.every((id) => selected.has(id))
                const isOpen = expanded.has(g.key)
                const modeOff = groupEnabled.length === 0
                const followedTotal = g.sources.reduce((acc, s) => acc + followedAccounts(s).length, 0)
                const GIcon = g.icon
                return (
                  <div key={g.key}>
                    {/* Header del grupo: tilda el grupo completo + expande sus fuentes. */}
                    <div className={cn("flex items-center gap-2.5 bg-muted/10 px-3 py-2", modeOff && "opacity-55")}>
                      <Switch
                        checked={allSel}
                        disabled={modeOff}
                        onCheckedChange={() => toggleGroup(groupEnabled, allSel)}
                        aria-label={`Seleccionar ${g.label}`}
                      />
                      <button
                        type="button"
                        onClick={() => toggleExpand(g.key)}
                        aria-expanded={isOpen}
                        className="flex min-w-0 flex-1 items-center gap-2 text-left"
                      >
                        <ChevronDown
                          className={cn(
                            "size-4 shrink-0 text-muted-foreground transition-transform",
                            !isOpen && "-rotate-90",
                          )}
                        />
                        <GIcon className={cn("size-4 shrink-0", g.tone)} />
                        <span className="text-sm font-medium">{g.label}</span>
                        {g.paid && (
                          <span
                            className="shrink-0"
                            title="Red social: la ingesta usa API de paga (Apify), tiene costo por corrida"
                          >
                            <TriangleAlert className="size-3 text-status-review" />
                          </span>
                        )}
                        <span className="truncate text-[11px] text-muted-foreground">
                          {modeOff
                            ? "solo modo incremental"
                            : `${g.sources.length} ${g.sources.length === 1 ? "fuente" : "fuentes"}${
                                followedTotal
                                  ? ` · ${followedTotal} ${followedTotal === 1 ? "cuenta seguida" : "cuentas seguidas"}`
                                  : ""
                              }`}
                        </span>
                        <span className="num ml-auto text-xs text-muted-foreground">
                          {selCount}/{groupEnabled.length || ids.length}
                        </span>
                      </button>
                    </div>

                    {isOpen &&
                      g.sources.map((s) => {
                        const enabled = supportsMode(s.type, mode)
                        const social = PAID_API_TYPES.has(s.type)
                        const cursor = checkpoints?.[s.id]
                        const progress = social ? socialAccountsProgress(cursor) : {}
                        const followed = social ? followedAccounts(s) : []
                        return (
                          <div
                            key={s.id}
                            className={cn("border-t border-border/60 py-2 pl-9 pr-3", !enabled && "opacity-55")}
                          >
                            {/* Fila de la fuente. El grupo ya dice el proveedor; acá va el alias. */}
                            <div className="flex items-center gap-2.5">
                              <Switch
                                checked={enabled && selected.has(s.id)}
                                disabled={!enabled}
                                onCheckedChange={() => toggle(s.id)}
                                aria-label={`Seleccionar ${sourceFullLabel(s)}`}
                              />
                              <button
                                type="button"
                                onClick={() => enabled && toggle(s.id)}
                                disabled={!enabled}
                                className="flex min-w-0 flex-1 items-center gap-2.5 text-left disabled:cursor-default"
                                title={s.name}
                              >
                                <div className="min-w-0">
                                  <span className="truncate text-sm font-medium">{rowLabel(s)}</span>
                                  <div className="truncate text-[11px] text-muted-foreground">
                                    {enabled
                                      ? checkpointLabel(cursor)
                                      : "Solo modo incremental (no admite rango/cantidad)"}
                                  </div>
                                </div>
                              </button>
                              <div className="shrink-0 pl-2">
                                <RowResultView r={results[s.id]} />
                              </div>
                            </div>

                            {/* Bloque social: estado del token + cuentas seguidas con su avance. */}
                            {social && (
                              <div className="mt-1.5 space-y-1 pl-12">
                                {s.tokenSource === "missing" ? (
                                  <p className="text-[11px] text-status-error">
                                    Sin token de Apify: cargalo en{" "}
                                    <Link to="/cuenta" className="underline">
                                      Cuenta
                                    </Link>{" "}
                                    (queda cifrado en el vault) o inyectá la env var en el servidor
                                    (Doppler). Sin token, traer va a fallar.
                                  </p>
                                ) : s.tokenSource ? (
                                  <p
                                    className="text-[10px] text-muted-foreground"
                                    title={
                                      s.tokenSource === "vault"
                                        ? "El token vive cifrado en la DB (vault de la cuenta vinculada) y pisa a la env var."
                                        : "El token llega como variable de entorno del servidor (p. ej. inyectada por Doppler); no está guardado en la DB."
                                    }
                                  >
                                    token de Apify:{" "}
                                    {s.tokenSource === "vault" ? "vault (cuenta vinculada)" : "env del servidor (Doppler)"}
                                  </p>
                                ) : null}

                                {followed.length === 0 ? (
                                  <p className="text-[11px] text-muted-foreground">
                                    Sin cuentas seguidas: esta fuente no trae nada. Agregá una abajo.
                                  </p>
                                ) : (
                                  followed.map((h) => {
                                    const key = `${s.id}:${h}`
                                    const at = progress[h]
                                    return (
                                      <div key={h} className="flex items-center gap-2">
                                        <AtSign className="size-3 shrink-0 text-muted-foreground" />
                                        <span className="num text-xs">{h}</span>
                                        <span className="truncate text-[11px] text-muted-foreground">
                                          {at ? `último post: ${formatRelative(at)}` : "sin avance todavía"}
                                        </span>
                                        <span className="ml-auto shrink-0">
                                          <RowResultView r={acctResults[key]} />
                                        </span>
                                        <Button
                                          variant="outline"
                                          size="xs"
                                          disabled={busy !== null || acctResults[key]?.status === "running"}
                                          onClick={() => void runAccount(s, h)}
                                          title="Trae lo nuevo SOLO de esta cuenta (una corrida de Apify, con costo). El avance de las demás no se toca."
                                        >
                                          <Download className="size-3" /> Traer
                                        </Button>
                                        <button
                                          type="button"
                                          disabled={mutBusy}
                                          title="Dejar de seguir esta cuenta"
                                          onClick={() => void removeAccount(s, h)}
                                          className="text-muted-foreground hover:text-status-error disabled:opacity-50"
                                        >
                                          <Trash2 className="size-3" />
                                        </button>
                                      </div>
                                    )
                                  })
                                )}

                                <div className="flex items-center gap-2 pt-0.5">
                                  <Input
                                    placeholder="handle, página o URL (p. ej. @nasa)"
                                    value={drafts[s.id] ?? ""}
                                    onChange={(e) => setDrafts((d) => ({ ...d, [s.id]: e.target.value }))}
                                    onKeyDown={(e) => {
                                      if (e.key === "Enter") void addAccount(s)
                                    }}
                                    className="h-7 max-w-72 text-xs"
                                  />
                                  <Button
                                    size="xs"
                                    variant="outline"
                                    disabled={mutBusy || !(drafts[s.id] ?? "").trim()}
                                    onClick={() => void addAccount(s)}
                                  >
                                    {mutBusy ? <Loader2 className="size-3 animate-spin" /> : <Plus className="size-3" />} Seguir
                                  </Button>
                                </div>
                              </div>
                            )}
                          </div>
                        )
                      })}
                  </div>
                )
              })}
            </div>
          </>
        )}
      </PanelBody>
    </Panel>
  )
}

function newDraftId(): string {
  return `manual:${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

export function AdHocIngest() {
  const { data: sources, loading, error, reload } = useAsync<Source[]>(() => fetchSources(), [])
  const [sourceId, setSourceId] = useState("")
  const [text, setText] = useState("")
  const [draftId, setDraftId] = useState(newDraftId)
  const [busy, setBusy] = useState<null | "dry" | "confirm">(null)
  const [result, setResult] = useState<{ would: boolean; reason: string } | null>(null)

  const selected = sourceId || (sources && sources[0] ? String(sources[0].id) : "")

  function parsePayload(): Record<string, unknown> | null {
    try {
      const v: unknown = JSON.parse(text)
      return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null
    } catch {
      return null
    }
  }

  async function dry() {
    const payload = parsePayload()
    if (!payload) {
      setResult({ would: false, reason: "el registro está vacío o el JSON no es válido" })
      return
    }
    setBusy("dry")
    try {
      const r = await ingestAdHoc(Number(selected), payload, { dryRun: true, externalId: draftId })
      setResult(
        r.would_insert
          ? { would: true, reason: "se guardará (no es duplicado ni filtrado)" }
          : { would: false, reason: `no se guardará — ${r.reason ?? "desconocido"}` },
      )
    } catch (e) {
      setResult({ would: false, reason: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  async function confirm() {
    const payload = parsePayload()
    if (!payload) return
    setBusy("confirm")
    try {
      const r = await ingestAdHoc(Number(selected), payload, { externalId: draftId })
      if (r.inserted) {
        toast.success("Registro guardado en el inbox", { description: r.id ? `id ${r.id}` : undefined })
        setText("")
        setResult(null)
        setDraftId(newDraftId())
      } else {
        toast.warning(`No se guardó — ${r.reason ?? "desconocido"}`)
        setResult({ would: false, reason: `no se guardó — ${r.reason ?? "desconocido"}` })
      }
    } catch (e) {
      toast.error("Error al guardar", { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  return (
    <CollapsiblePanel
      eyebrow="ingesta · puntual"
      title="Ingesta ad-hoc"
      sub="Inyectá un registro manual; el dry-run valida (duplicado/filtrado) antes de confirmar"
      right={<CapBadge level="existe" title="Valida con dry-run antes de guardar" />}
      bodyClassName="space-y-3"
    >
      {error ? (
        <ErrorState detail={error} onRetry={reload} />
      ) : loading ? (
        <div className="flex items-center gap-2 px-2 py-8 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Cargando fuentes…
        </div>
      ) : !sources || sources.length === 0 ? (
        <EmptyState title="No hay fuentes" hint="Creá una fuente para inyectar registros." />
      ) : (
        <>
          <Field label="Fuente">
            <SourceSelect sources={sources} value={selected} onChange={setSourceId} />
          </Field>
          <Field label="Registro (JSON)">
            <textarea
              name="adhoc-json"
              value={text}
              onChange={(e) => { setText(e.target.value); setResult(null) }}
              rows={5}
              placeholder={'{"from":{"email":"ana@x.com"},"subject":"Recibo","body_text":"Total: $123"}'}
              className="w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring"
            />
          </Field>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={busy !== null || !selected} onClick={dry}>
              {busy === "dry" ? <Loader2 className="size-3.5 animate-spin" /> : <FlaskConical className="size-3.5" />} Dry-run
            </Button>
            <Button size="sm" disabled={!result?.would || busy !== null} onClick={confirm}>
              {busy === "confirm" ? <Loader2 className="size-3.5 animate-spin" /> : null} Confirmar
            </Button>
          </div>
          {result && (
            <div
              className={cn(
                "rounded-md border p-2 text-xs",
                result.would
                  ? "border-status-ok/30 bg-status-ok/10 text-status-ok"
                  : "border-status-review/30 bg-status-review/10 text-status-review",
              )}
            >
              {result.would ? "✓ " : "✕ "}
              {result.reason}
            </div>
          )}
        </>
      )}
    </CollapsiblePanel>
  )
}
