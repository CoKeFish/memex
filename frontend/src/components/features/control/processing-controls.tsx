import { useState } from "react"
import { FlaskConical, Play, Power } from "lucide-react"
import { toast } from "sonner"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Switch } from "@/components/ui/switch"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { CapBadge } from "@/components/common/cap-badge"
import { RelativeTime } from "@/components/common/time"
import { formatPct } from "@/lib/format"
import {
  dryRunRun,
  getModuleSettings,
  getSchedulerEnabled,
  getSchedulerJobs,
  getSources,
  JOBS,
  JOB_LABEL,
} from "@/data"
import type { RunPreview, WorkerJob } from "@/types/domain"

export function SchedulerPanel() {
  const [enabled, setEnabled] = useState(getSchedulerEnabled())
  const jobs = getSchedulerJobs()
  return (
    <Panel>
      <PanelHeader
        eyebrow="procesamiento · automático"
        title="Scheduler"
        sub="El daemon corre los workers idempotentes en intervalos"
        right={<CapBadge level="parcial" title="hoy por env/config; sin toggle en runtime ni HTTP" />}
      />
      <PanelBody className="space-y-3">
        <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-background/40 p-3">
          <div className="flex items-center gap-2.5">
            <Power className={cn("size-4", enabled ? "text-status-ok" : "text-muted-foreground")} />
            <div>
              <div className="text-sm font-medium">Procesamiento automático</div>
              <div className="text-xs text-muted-foreground">
                {enabled ? "corriendo en intervalos" : "apagado — nada procesa solo; corré los pasos a mano abajo"}
              </div>
            </div>
          </div>
          <Switch checked={enabled} onCheckedChange={setEnabled} />
        </div>
        <ul className="divide-y divide-border rounded-md border border-border">
          {jobs.map((j) => (
            <li key={j.job} className="flex items-center justify-between gap-3 px-3 py-2">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{JOB_LABEL[j.job]}</span>
                <span className="num text-[11px] text-muted-foreground">{j.cron}</span>
              </div>
              <div className="num flex items-center gap-3 text-[11px] text-muted-foreground">
                {j.lastRun && (
                  <span>
                    última <RelativeTime date={j.lastRun} />
                  </span>
                )}
                <StatusBadge tone={j.enabled ? "ok" : "neutral"} label={j.enabled ? "on" : "off"} />
              </div>
            </li>
          ))}
        </ul>
      </PanelBody>
    </Panel>
  )
}

export function ManualRunPanel() {
  const [open, setOpen] = useState<WorkerJob | null>(null)
  const [preview, setPreview] = useState<RunPreview | null>(null)
  return (
    <Panel>
      <PanelHeader
        eyebrow="procesamiento · manual"
        title="Correr pasos a mano"
        sub="Cada etapa es idempotente (cursor por ausencia de fila); dry-run antes de gastar"
        right={<CapBadge level="existe" title="vía CLI hoy; endpoints HTTP /run son futuro" />}
      />
      <PanelBody className="space-y-2">
        {JOBS.map((j) => (
          <div key={j.key} className="overflow-hidden rounded-md border border-border">
            <div className="flex items-center justify-between gap-2 px-3 py-2">
              <span className="text-sm font-medium">{j.label}</span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  className="h-7"
                  onClick={() => { setOpen(j.key); setPreview(dryRunRun(j.key)) }}
                >
                  <FlaskConical className="size-3.5" /> Dry-run
                </Button>
                <Button size="sm" className="h-7" onClick={() => toast.success(`${j.label}: corrida encolada`)}>
                  <Play className="size-3.5" /> Ejecutar
                </Button>
              </div>
            </div>
            {open === j.key && preview && (
              <div className="space-y-2 border-t border-border bg-muted/20 p-3 text-xs">
                <div className="flex flex-wrap gap-1.5">
                  {preview.estimate.map((e, i) => (
                    <span key={i} className="num rounded bg-muted/60 px-1.5 py-0.5">
                      <span className="text-muted-foreground">{e.label}</span> {e.value}
                    </span>
                  ))}
                </div>
                <pre className="overflow-x-auto rounded border border-border bg-background px-2 py-1 font-mono text-[11px]">
                  {preview.command}
                </pre>
              </div>
            )}
          </div>
        ))}
      </PanelBody>
    </Panel>
  )
}

export function SourcesTogglePanel() {
  const sources = getSources()
  const [state, setState] = useState<Record<number, boolean>>(() => Object.fromEntries(sources.map((s) => [s.id, s.enabled])))
  return (
    <Panel>
      <PanelHeader
        eyebrow="procesamiento · fuentes"
        title="Fuentes"
        sub="Habilitar/deshabilitar la ingesta por fuente (sources.enabled)"
        right={<CapBadge level="parcial" title="columna existe; falta endpoint de mutación" />}
      />
      <PanelBody className="space-y-1.5">
        {sources.map((s) => (
          <div key={s.id} className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2">
            <div>
              <div className="text-sm font-medium">{s.name}</div>
              <div className="eyebrow">{s.type}</div>
            </div>
            <Switch checked={state[s.id]} onCheckedChange={(c) => setState((p) => ({ ...p, [s.id]: c }))} />
          </div>
        ))}
      </PanelBody>
    </Panel>
  )
}

export function ModulesTogglePanel() {
  const mods = getModuleSettings()
  const [enabled, setEnabled] = useState<Record<string, boolean>>(() => Object.fromEntries(mods.map((m) => [m.slug, m.enabled])))
  return (
    <Panel>
      <PanelHeader
        eyebrow="procesamiento · módulos"
        title="Módulos de extracción"
        sub="Habilitar + política de batching por módulo (module_settings)"
        right={<CapBadge level="parcial" title="columnas existen; perillas vía flags de CLI" />}
      />
      <PanelBody className="space-y-2">
        {mods.map((m) => {
          const pct = m.total ? m.processed / m.total : 0
          return (
            <div key={m.slug} className="rounded-md border border-border p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium">{m.label}</span>
                <Switch checked={enabled[m.slug]} onCheckedChange={(c) => setEnabled((p) => ({ ...p, [m.slug]: c }))} />
              </div>
              <div className="num mt-2 flex flex-wrap gap-1.5 text-[11px] text-muted-foreground">
                <span className="rounded bg-muted/60 px-1.5 py-0.5">policy {m.batchingPolicy}</span>
                <span className="rounded bg-muted/60 px-1.5 py-0.5">group_size {m.groupSize}</span>
              </div>
              <div className="mt-2">
                <div className="mb-0.5 flex justify-between text-[11px] text-muted-foreground">
                  <span>cobertura de extracción</span>
                  <span className="num">{formatPct(pct, 0)}</span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                  <div className="h-full rounded-full bg-brand" style={{ width: `${pct * 100}%` }} />
                </div>
              </div>
            </div>
          )
        })}
      </PanelBody>
    </Panel>
  )
}
