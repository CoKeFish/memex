import { useState } from "react"
import { Download, FlaskConical, Loader2 } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { CapBadge, type CapLevel } from "@/components/common/cap-badge"
import { formatInt } from "@/lib/format"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import { fetchEmailSources, fetchSources, ingestAdHoc, triggerFetch } from "@/data"
import type { FetchPreview, Source } from "@/types/domain"

type Mode = "incremental" | "range" | "last"
const MODES: { v: Mode; label: string; cap: CapLevel }[] = [
  { v: "incremental", label: "Incremental (checkpoint)", cap: "existe" },
  { v: "range", label: "Rango de fechas", cap: "existe" },
  { v: "last", label: "Últimos N", cap: "existe" },
]

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
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
          <SelectItem key={s.id} value={String(s.id)} className="text-sm">
            {s.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function PreviewView({ p }: { p: FetchPreview }) {
  const cells = [
    { label: "escaneados", value: p.scanned, cls: "text-foreground" },
    { label: "nuevos", value: p.nuevos, cls: "text-status-ok" },
    { label: "ya existentes", value: p.duplicados, cls: "text-muted-foreground" },
    { label: "filtrados", value: p.filtrados, cls: "text-status-filtered" },
  ]
  return (
    <div className="rounded-md border border-border bg-muted/30 p-3">
      <div className="grid grid-cols-4 gap-2 text-center">
        {cells.map((c) => (
          <div key={c.label}>
            <div className={cn("num text-lg font-semibold", c.cls)}>{formatInt(c.value)}</div>
            <div className="eyebrow mt-0.5">{c.label}</div>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[11px] text-muted-foreground">
        Los <span className="text-foreground">{formatInt(p.duplicados)} ya existentes</span> se ignoran: dedup por{" "}
        <span className="num">UNIQUE(source_id, external_id)</span> + checkpoint. No se insertan duplicados.
      </p>
    </div>
  )
}

export function FetchControl() {
  const { data: sources, loading, error, reload } = useAsync<Source[]>(() => fetchEmailSources(), [])
  const [sourceId, setSourceId] = useState("")
  const [mode, setMode] = useState<Mode>("incremental")
  const [n, setN] = useState(50)
  const [since, setSince] = useState("")
  const [until, setUntil] = useState("")
  const [preview, setPreview] = useState<FetchPreview | null>(null)
  const [busy, setBusy] = useState<null | "dry" | "run">(null)

  const modeMeta = MODES.find((m) => m.v === mode)!
  const selected = sourceId || (sources && sources[0] ? String(sources[0].id) : "")

  async function run(dryRun: boolean) {
    if (!selected) return
    setBusy(dryRun ? "dry" : "run")
    try {
      const r = await triggerFetch(Number(selected), {
        dryRun,
        mode,
        since: mode === "range" ? since || undefined : undefined,
        until: mode === "range" ? until || undefined : undefined,
        limit: mode === "last" ? n : undefined,
      })
      const p: FetchPreview = {
        scanned: r.posted,
        nuevos: r.inserted,
        duplicados: r.duplicates,
        filtrados: r.filtered,
      }
      setPreview(p)
      if (!dryRun) {
        toast.success("Corrida de ingesta terminada", {
          description: `${p.nuevos} nuevos · ${p.duplicados} ya existentes (ignorados) · ${p.filtrados} filtrados`,
        })
      }
    } catch (e) {
      toast.error("Falló la ingesta", { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  const missingRange = mode === "range" && !since
  const disabled = busy !== null || !selected || missingRange

  return (
    <Panel>
      <PanelHeader
        eyebrow="ingesta · fetch"
        title="Traer correos a demanda"
        sub="Dispará una corrida de ingesta; el dry-run muestra cuántos son nuevos vs ya guardados"
        right={<CapBadge level={modeMeta.cap} title="incremental avanza el checkpoint; rango/últimos N son backfills que no lo tocan" />}
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
            title="No hay fuentes de correo"
            hint="Creá una fuente imap (POST /sources) para poder traer correos."
          />
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label="Fuente">
                <SourceSelect
                  sources={sources}
                  value={selected}
                  onChange={(v) => {
                    setSourceId(v)
                    setPreview(null)
                  }}
                />
              </Field>
              <Field label="Modo">
                <Select value={mode} onValueChange={(v) => { setMode(v as Mode); setPreview(null) }}>
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
                Trae lo nuevo desde el último checkpoint (la 1ª vez, los últimos {" "}
                <span className="num">since_days</span>) y lo avanza.
              </p>
            )}
            {mode === "range" && (
              <p className="text-xs text-muted-foreground">
                Backfill de la ventana <span className="num">since..until</span> (los más recientes primero).
                No mueve el checkpoint incremental.
              </p>
            )}
            {mode === "last" && (
              <p className="text-xs text-muted-foreground">
                Backfill de los <span className="num">{n}</span> mensajes más recientes. No mueve el checkpoint.
              </p>
            )}
            {missingRange && (
              <p className="text-xs text-status-review">Indicá al menos la fecha “Desde”.</p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm" disabled={disabled} onClick={() => run(true)}>
                {busy === "dry" ? <Loader2 className="size-3.5 animate-spin" /> : <FlaskConical className="size-3.5" />} Dry-run
              </Button>
              <Button size="sm" disabled={disabled} onClick={() => run(false)}>
                {busy === "run" ? <Loader2 className="size-3.5 animate-spin" /> : <Download className="size-3.5" />} Traer ahora
              </Button>
            </div>
            {preview && <PreviewView p={preview} />}
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
      setResult({ would: false, reason: "payload vacío o JSON inválido" })
      return
    }
    setBusy("dry")
    try {
      const r = await ingestAdHoc(Number(selected), payload, { dryRun: true, externalId: draftId })
      setResult(
        r.would_insert
          ? { would: true, reason: "se insertará (no es duplicado, no filtrado)" }
          : { would: false, reason: `no se insertará — ${r.reason ?? "desconocido"}` },
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
        toast.success("Registro insertado en inbox", { description: r.id ? `id ${r.id}` : undefined })
        setText("")
        setResult(null)
        setDraftId(newDraftId())
      } else {
        toast.warning(`No insertado — ${r.reason ?? "desconocido"}`)
        setResult({ would: false, reason: `no se insertó — ${r.reason ?? "desconocido"}` })
      }
    } catch (e) {
      toast.error("Error al insertar", { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  return (
    <Panel>
      <PanelHeader
        eyebrow="ingesta · puntual"
        title="Ingesta ad-hoc"
        sub="Inyectá un registro manual; X-Dry-Run valida (duplicado/filtrado) antes de confirmar"
        right={<CapBadge level="existe" title="POST /ingest con header X-Dry-Run" />}
      />
      <PanelBody className="space-y-3">
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading ? (
          <div className="flex items-center gap-2 px-2 py-8 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Cargando fuentes…
          </div>
        ) : !sources || sources.length === 0 ? (
          <EmptyState title="No hay fuentes" hint="Creá una fuente (POST /sources) para inyectar registros." />
        ) : (
          <>
            <Field label="Fuente">
              <SourceSelect sources={sources} value={selected} onChange={setSourceId} />
            </Field>
            <Field label="Payload (JSON)">
              <textarea
                value={text}
                onChange={(e) => { setText(e.target.value); setResult(null) }}
                rows={5}
                placeholder={'{"from":{"email":"ana@x.com"},"subject":"Recibo","body_text":"Total: $123"}'}
                className="w-full rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
      </PanelBody>
    </Panel>
  )
}
