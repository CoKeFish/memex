import { useState } from "react"
import { Download, FlaskConical } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { CapBadge, type CapLevel } from "@/components/common/cap-badge"
import { formatInt } from "@/lib/format"
import { dryRunFetch, getSources } from "@/data"
import type { FetchPreview } from "@/types/domain"

type Mode = "incremental" | "range" | "last"
const MODES: { v: Mode; label: string; cap: CapLevel }[] = [
  { v: "incremental", label: "Incremental (checkpoint)", cap: "existe" },
  { v: "range", label: "Rango de fechas", cap: "futuro" },
  { v: "last", label: "Últimos N", cap: "futuro" },
]

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="eyebrow mb-1 block">{label}</span>
      {children}
    </label>
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
  const sources = getSources().filter((s) => s.type !== "calendar")
  const [sourceId, setSourceId] = useState(String(sources[0]?.id ?? 1))
  const [mode, setMode] = useState<Mode>("incremental")
  const [n, setN] = useState(50)
  const [since, setSince] = useState("")
  const [until, setUntil] = useState("")
  const [preview, setPreview] = useState<FetchPreview | null>(null)

  const modeMeta = MODES.find((m) => m.v === mode)!
  const compute = () => dryRunFetch(Number(sourceId), mode, n)

  return (
    <Panel>
      <PanelHeader
        eyebrow="ingesta · fetch"
        title="Traer correos a demanda"
        sub="Dispará una corrida de ingesta; el dry-run muestra cuántos son nuevos vs ya guardados"
        right={<CapBadge level={modeMeta.cap} title="incremental existe vía CLI; rango/N requieren flags nuevos" />}
      />
      <PanelBody className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Fuente">
            <Select value={sourceId} onValueChange={(v) => { setSourceId(v); setPreview(null) }}>
              <SelectTrigger className="h-9 text-sm"><SelectValue /></SelectTrigger>
              <SelectContent>
                {sources.map((s) => (
                  <SelectItem key={s.id} value={String(s.id)} className="text-sm">{s.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
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
        {mode !== "incremental" && (
          <p className="text-xs text-status-review">
            El ingestor hoy es incremental por checkpoint; <span className="num">rango/cantidad</span> requiere flags nuevos
            (<span className="num">--since</span>/<span className="num">--limit</span>) en el CLI o un endpoint.
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => setPreview(compute())}>
            <FlaskConical className="size-3.5" /> Dry-run
          </Button>
          <Button
            size="sm"
            onClick={() => {
              const p = compute()
              toast.success("Corrida de ingesta encolada", {
                description: `${p.nuevos} nuevos · ${p.duplicados} ya existentes (ignorados) · ${p.filtrados} filtrados`,
              })
            }}
          >
            <Download className="size-3.5" /> Traer ahora
          </Button>
        </div>
        {preview && <PreviewView p={preview} />}
      </PanelBody>
    </Panel>
  )
}

export function AdHocIngest() {
  const sources = getSources()
  const [sourceId, setSourceId] = useState(String(sources[0]?.id ?? 1))
  const [text, setText] = useState("")
  const [result, setResult] = useState<{ would: boolean; reason: string } | null>(null)

  function dry() {
    const t = text.toLowerCase()
    if (text.trim().length < 5) setResult({ would: false, reason: "payload vacío o inválido" })
    else if (t.includes("unsubscribe") || t.includes("list-unsubscribe"))
      setResult({ would: false, reason: "filtrado por filter_rules (list_unsubscribe → ignore)" })
    else if (t.includes('"duplicate"') || t.includes("uid:10001"))
      setResult({ would: false, reason: "duplicado: ya existe ese external_id (dedupe)" })
    else setResult({ would: true, reason: "se insertará (no es duplicado, no filtrado)" })
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
        <Field label="Fuente">
          <Select value={sourceId} onValueChange={setSourceId}>
            <SelectTrigger className="h-9 text-sm"><SelectValue /></SelectTrigger>
            <SelectContent>
              {sources.map((s) => (
                <SelectItem key={s.id} value={String(s.id)} className="text-sm">{s.name}</SelectItem>
              ))}
            </SelectContent>
          </Select>
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
          <Button variant="outline" size="sm" onClick={dry}>
            <FlaskConical className="size-3.5" /> Dry-run
          </Button>
          <Button size="sm" disabled={!result?.would} onClick={() => toast.success("Registro insertado en inbox")}>
            Confirmar
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
      </PanelBody>
    </Panel>
  )
}
