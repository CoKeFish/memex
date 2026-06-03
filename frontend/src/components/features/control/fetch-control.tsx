import { useEffect, useMemo, useRef, useState } from "react"
import { Link } from "react-router-dom"
import { ArrowRight, Download, FlaskConical, Loader2, TriangleAlert } from "lucide-react"
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
import { formatDate, formatInt } from "@/lib/format"
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
import type { FetchPreview, Source } from "@/types/domain"
import { BackfillSection } from "./backfill-control"

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

/** Estado de la fuente en lenguaje claro a partir del cursor crudo (oculta llaves internas como
 * uidvalidity). Formas conocidas: imap → {folders:{INBOX:{last_uid,uidvalidity}}}; push (outlook)
 * → {last_received_at}. */
function checkpointLabel(cursor: Record<string, unknown> | null | undefined): string {
  if (!cursor) return "Aún no se ha traído nada de esta fuente."
  // imap: máximo last_uid entre las carpetas seguidas.
  const folders = cursor.folders
  if (folders && typeof folders === "object") {
    const uids = Object.values(folders as Record<string, unknown>)
      .map((f) => (f && typeof f === "object" ? Number((f as Record<string, unknown>).last_uid) : NaN))
      .filter((u) => Number.isFinite(u))
    if (uids.length) return `Último correo traído: #${Math.max(...uids)}.`
  }
  if (typeof cursor.last_uid === "number") return `Último correo traído: #${cursor.last_uid}.`
  if (typeof cursor.last_received_at === "string") {
    return `Último correo recibido: ${formatDate(cursor.last_received_at)}.`
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

function SourceSelect({
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

/** Resultado compacto por fila tras correr el fetch sobre esa fuente. */
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

export function FetchControl() {
  const { data: sources, loading, error, reload } = useAsync<Source[]>(() => fetchPullableSources(), [])
  const [mode, setMode] = useState<Mode>("incremental")
  const [n, setN] = useState(50)
  const [since, setSince] = useState("")
  const [until, setUntil] = useState("")
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [results, setResults] = useState<Record<number, RowResult>>({})
  const [busy, setBusy] = useState<null | "dry" | "run">(null)
  const [ranReal, setRanReal] = useState(false)

  const modeMeta = MODES.find((m) => m.v === mode)!
  const allIds = useMemo(() => (sources ?? []).map((s) => s.id), [sources])
  // Fuentes que el modo actual puede traer (incremental = todas; rango/últimos N = solo correo).
  const enabledIds = useMemo(
    () => (sources ?? []).filter((s) => supportsMode(s.type, mode)).map((s) => s.id),
    [sources, mode],
  )
  // Fuente de correo sobre la que opera la Importación masiva: la primera imap TILDADA (opción 2).
  const activeImapId = useMemo(
    () => (sources ?? []).find((s) => s.type === "imap" && selected.has(s.id))?.id ?? null,
    [sources, selected],
  )

  // Punto guardado de cada fuente (para la línea bajo cada fila).
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
  }, [sources])

  // Selección inicial: todas las fuentes, una sola vez al cargar.
  const initRef = useRef(false)
  useEffect(() => {
    if (!initRef.current && sources && sources.length) {
      setSelected(new Set(sources.map((s) => s.id)))
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
    const verb = dryRun ? "Dry-run" : "Ingesta"
    const desc = `${total.nuevos} nuevos · ${total.duplicados} ya existentes · ${total.filtrados} filtrados`
    if (err === 0) {
      if (!dryRun) setRanReal(true)
      toast.success(`${verb}: ${ok} ${ok === 1 ? "fuente" : "fuentes"}`, { description: desc })
    } else {
      if (!dryRun && ok > 0) setRanReal(true)
      toast.warning(`${verb}: ${ok} ok · ${err} con error`, { description: desc })
    }
  }

  const missingRange = mode === "range" && !since
  const disabled = busy !== null || selected.size === 0 || missingRange
  // Atajo: si corrió una sola fuente, filtro por ella; si fueron varias, la vista completa.
  const ranIds = allIds.filter((id) => selected.has(id))
  const datosHref = ranIds.length === 1 ? `/datos?source=${ranIds[0]}` : "/datos"

  return (
    <Panel>
      <PanelHeader
        eyebrow="ingesta · fetch"
        title="Traer a demanda"
        sub="Elegí el modo y tildá una o varias fuentes (correo, Telegram, redes); el dry-run muestra cuántos son nuevos vs ya guardados"
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
                Descarga histórica del rango elegido (los más recientes primero). No afecta el avance
                del modo incremental.
              </p>
            )}
            {mode === "last" && (
              <p className="text-xs text-muted-foreground">
                Descarga los <span className="num">{n}</span> mensajes más recientes por fuente. No
                afecta el avance del modo incremental.
              </p>
            )}
            {missingRange && (
              <p className="text-xs text-status-review">Indicá al menos la fecha “Desde”.</p>
            )}

            {/* Lista de fuentes (mitad izq) + acciones del fetch + Importación masiva (mitad der). */}
            <div className="flex flex-col gap-4 sm:flex-row sm:items-start">
              <div className="min-w-0 flex-1 divide-y divide-border overflow-hidden rounded-md border border-border">
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
                {sources.map((s) => {
                  const m = sourceMeta(s)
                  const Icon = m.icon
                  const enabled = supportsMode(s.type, mode)
                  return (
                    <div
                      key={s.id}
                      className={cn("flex items-center gap-2.5 px-3 py-2", !enabled && "opacity-55")}
                    >
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
                      >
                        <Icon className={cn("size-4 shrink-0", m.tone)} />
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5">
                            <span className="truncate text-sm font-medium">{sourceFullLabel(s)}</span>
                            {PAID_API_TYPES.has(s.type) && (
                              <span
                                className="shrink-0"
                                title="Red social: la ingesta usa API de paga (Apify), tiene costo por corrida"
                              >
                                <TriangleAlert className="size-3 text-status-review" />
                              </span>
                            )}
                          </div>
                          <div className="truncate text-[11px] text-muted-foreground">
                            {enabled
                              ? checkpointLabel(checkpoints?.[s.id])
                              : "Solo modo incremental (no admite rango/cantidad)"}
                          </div>
                        </div>
                      </button>
                      <div className="shrink-0 pl-2">
                        <RowResultView r={results[s.id]} />
                      </div>
                    </div>
                  )
                })}
              </div>

              {/* Mitad derecha: acciones del fetch arriba, Importación masiva (backfill) abajo. */}
              <div className="min-w-0 flex-1 space-y-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="outline" size="sm" disabled={disabled} onClick={() => run(true)}>
                    {busy === "dry" ? <Loader2 className="size-3.5 animate-spin" /> : <FlaskConical className="size-3.5" />} Dry-run
                  </Button>
                  <Button size="sm" disabled={disabled} onClick={() => run(false)}>
                    {busy === "run" ? <Loader2 className="size-3.5 animate-spin" /> : <Download className="size-3.5" />}
                    {selected.size > 1 ? `Traer ${selected.size} ahora` : "Traer ahora"}
                  </Button>
                </div>

                {okCount > 0 && (
                  <PreviewView p={agg} compact title={okCount > 1 ? `total · ${okCount} fuentes` : undefined} />
                )}
                {ranReal && (
                  <Link
                    to={datosHref}
                    className="inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline"
                  >
                    Ver en datos <ArrowRight className="size-3.5" />
                  </Link>
                )}

                {/* Importación masiva (backfill) sobre la fuente de correo tildada en la lista. */}
                <div className="border-t border-border pt-3">
                  <div className="eyebrow mb-2">Importación masiva</div>
                  <BackfillSection sourceId={activeImapId} />
                </div>
              </div>
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
