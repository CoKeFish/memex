import { useState } from "react"
import { Loader2, Play, Plus, Send, Trash2 } from "lucide-react"
import { toast } from "sonner"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import { cn } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Switch } from "@/components/ui/switch"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { ErrorState } from "@/components/common/data-state"
import { RelativeTime } from "@/components/common/time"
import {
  addNotificationChannel,
  deleteNotificationChannel,
  fetchNotificationDeliveries,
  fetchNotificationSettings,
  patchNotificationChannel,
  patchNotificationProducer,
  patchNotificationSettings,
  runNotificationsCycle,
  testNotificationChannel,
  type NotificationChannel,
  type NotificationConfigValue,
  type NotificationProducer,
  type NotificationRunResult,
} from "@/data"
import type { AlertSeverity, NotificationAudience } from "@/types/domain"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

const SEVERITIES: AlertSeverity[] = ["info", "alta", "critica"]
const OFF = "__off__"
const AUDIENCE_LABEL: Record<NotificationAudience, string> = {
  personal: "personal",
  sistema: "sistema",
}

function configToText(v: NotificationConfigValue): string {
  return Array.isArray(v) ? v.join(",") : String(v)
}

/** Una fila de productor: activo · redacción · claves de config editables (se guardan al salir). */
function ProducerRow({
  p,
  onChanged,
}: {
  p: NotificationProducer
  onChanged: () => void
}) {
  const [draft, setDraft] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(p.config).map(([k, v]) => [k, configToText(v)])),
  )
  const [busy, setBusy] = useState(false)

  const save = async (patch: Parameters<typeof patchNotificationProducer>[1]) => {
    setBusy(true)
    try {
      await patchNotificationProducer(p.name, patch)
      onChanged()
    } catch (e) {
      toast.error(`${p.name}: ${errMsg(e)}`)
    } finally {
      setBusy(false)
    }
  }

  const commitKey = (key: string) => {
    const current = configToText(p.config[key])
    if (draft[key] === current) return
    const def = p.defaultConfig[key]
    const raw = draft[key]
    const value: NotificationConfigValue = Array.isArray(def)
      ? raw.split(",").map((s) => s.trim()).filter(Boolean)
      : typeof def === "number"
        ? Number(raw)
        : typeof def === "boolean"
          ? /^(1|si|sí|true|on|yes)$/i.test(raw.trim())
          : raw
    void save({ config: { [key]: value } })
  }

  return (
    <li className={cn("flex flex-col gap-2 px-4 py-3", !p.enabled && "opacity-60")}>
      <div className="flex items-center gap-3">
        <Switch
          checked={p.enabled}
          disabled={busy}
          onCheckedChange={(v) => void save({ enabled: v })}
          aria-label={`Activar ${p.name}`}
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium">{p.name}</span>
            <span className="rounded border px-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
              {AUDIENCE_LABEL[p.audience]}
            </span>
          </div>
          <p className="text-xs text-muted-foreground">{p.description}</p>
        </div>
        {p.llmAllowed ? (
          <Select
            value={p.render}
            onValueChange={(v) => void save({ render: v as NotificationProducer["render"] })}
          >
            <SelectTrigger className="h-8 w-[120px] text-xs" aria-label="Redacción">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="plantilla" className="text-xs">plantilla</SelectItem>
              <SelectItem value="llm" className="text-xs">llm</SelectItem>
            </SelectContent>
          </Select>
        ) : (
          <span className="w-[120px] text-right text-xs text-muted-foreground">plantilla</span>
        )}
      </div>
      <div className="flex flex-wrap gap-3 pl-11">
        {Object.keys(p.defaultConfig).map((key) => (
          <label key={key} className="flex items-center gap-1.5 text-xs text-muted-foreground">
            {key}
            <Input
              className="h-7 w-[120px] text-xs"
              value={draft[key] ?? ""}
              disabled={busy}
              onChange={(e) => setDraft((d) => ({ ...d, [key]: e.target.value }))}
              onBlur={() => commitKey(key)}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur()
              }}
            />
          </label>
        ))}
      </div>
    </li>
  )
}

function SeveritySelect({
  value,
  onChange,
  label,
}: {
  value: AlertSeverity | undefined
  onChange: (v: AlertSeverity | undefined) => void
  label: string
}) {
  return (
    <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
      {label}
      <Select
        value={value ?? OFF}
        onValueChange={(v) => onChange(v === OFF ? undefined : (v as AlertSeverity))}
      >
        <SelectTrigger className="h-7 w-[100px] text-xs" aria-label={label}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={OFF} className="text-xs">no</SelectItem>
          {SEVERITIES.map((s) => (
            <SelectItem key={s} value={s} className="text-xs">≥ {s}</SelectItem>
          ))}
        </SelectContent>
      </Select>
    </label>
  )
}

function ChannelRow({ c, onChanged }: { c: NotificationChannel; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  const run = async (fn: () => Promise<unknown>, okMsg?: string) => {
    setBusy(true)
    try {
      await fn()
      if (okMsg) toast.success(okMsg)
      onChanged()
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setBusy(false)
    }
  }
  const setSub = (audience: NotificationAudience, v: AlertSeverity | undefined) => {
    const subs = { ...c.subscriptions }
    if (v === undefined) delete subs[audience]
    else subs[audience] = v
    void run(() => patchNotificationChannel(c.id, { subscriptions: subs }))
  }
  return (
    <li className={cn("flex flex-wrap items-center gap-3 px-4 py-3", !c.enabled && "opacity-60")}>
      <Switch
        checked={c.enabled}
        disabled={busy}
        onCheckedChange={(v) => void run(() => patchNotificationChannel(c.id, { enabled: v }))}
        aria-label={`Activar canal ${c.channel}`}
      />
      <div className="min-w-[140px]">
        <div className="text-sm font-medium">{c.channel}</div>
        <div className="text-xs text-muted-foreground">{c.target || "—"}</div>
      </div>
      <SeveritySelect label="personal" value={c.subscriptions.personal} onChange={(v) => setSub("personal", v)} />
      <SeveritySelect label="sistema" value={c.subscriptions.sistema} onChange={(v) => setSub("sistema", v)} />
      <span className="text-xs text-muted-foreground">
        {c.quietFrom && c.quietUntil ? `silencio ${c.quietFrom}–${c.quietUntil}` : "sin silencio"}
      </span>
      <div className="ml-auto flex items-center gap-1">
        <Button
          variant="outline"
          size="sm"
          className="h-7 text-xs"
          disabled={busy}
          onClick={() => void run(() => testNotificationChannel(c.id), "Prueba enviada")}
        >
          <Send className="size-3" /> Probar
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="size-7"
          aria-label="Quitar canal"
          disabled={busy}
          onClick={() => void run(() => deleteNotificationChannel(c.id), "Canal quitado")}
        >
          <Trash2 className="size-3.5" />
        </Button>
      </div>
    </li>
  )
}

function AddChannel({ names, onAdded }: { names: string[]; onAdded: () => void }) {
  const [channel, setChannel] = useState(names.includes("telegram") ? "telegram" : names[0] ?? "")
  const [target, setTarget] = useState("")
  const [quiet, setQuiet] = useState("")
  const [busy, setBusy] = useState(false)
  const submit = async () => {
    setBusy(true)
    try {
      const [qf, qu] = quiet.includes("-") ? quiet.split("-", 2).map((s) => s.trim()) : [null, null]
      await addNotificationChannel({
        channel,
        target,
        subscriptions: { personal: "info", sistema: "critica" },
        quietFrom: qf,
        quietUntil: qu,
      })
      setTarget("")
      setQuiet("")
      toast.success("Canal agregado")
      onAdded()
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="flex flex-wrap items-center gap-2 border-t px-4 py-3">
      <Select value={channel} onValueChange={setChannel}>
        <SelectTrigger className="h-8 w-[120px] text-xs" aria-label="Canal">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {names.map((n) => (
            <SelectItem key={n} value={n} className="text-xs">{n}</SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Input
        className="h-8 w-[160px] text-xs"
        placeholder={channel === "telegram" ? "chat_id" : "destino"}
        value={target}
        onChange={(e) => setTarget(e.target.value)}
      />
      <Input
        className="h-8 w-[130px] text-xs"
        placeholder="silencio 22:00-07:00"
        value={quiet}
        onChange={(e) => setQuiet(e.target.value)}
      />
      <Button size="sm" className="h-8 text-xs" disabled={busy || !channel} onClick={() => void submit()}>
        {busy ? <Loader2 className="size-3 animate-spin" /> : <Plus className="size-3" />} Agregar
      </Button>
    </div>
  )
}

function RunResult({ r }: { r: NotificationRunResult }) {
  return (
    <div className="space-y-1 rounded border bg-muted/30 p-3 text-xs">
      <div>
        productores {r.producers.join(", ") || "—"} · borradores {r.drafts} · encolados {r.enqueued} ·
        ya existían {r.skippedExisting} · llm {r.renderedLlm} · pendientes {r.pending} · entregados{" "}
        {r.delivered} · fallidos {r.failed} · silencio {r.heldQuiet}
      </div>
      {r.preview.map((p, i) => (
        <div key={i} className="whitespace-pre-wrap text-muted-foreground">
          {"deliver" in p
            ? `→ ${String(p.deliver)}: ${String(p.title)}`
            : `[${String(p.producer)} · ${String(p.render)}] ${String(p.title)}\n${String(p.body)}`}
        </div>
      ))}
      {r.errors.map((e, i) => (
        <div key={i} className="text-destructive">{e}</div>
      ))}
    </div>
  )
}

/** Configuración del daemon de notificaciones: prendido/zona, productores, canales, entregas. */
export function NotificationsSettings() {
  const settings = useAsync(() => fetchNotificationSettings(), [])
  const deliveries = useAsync(() => fetchNotificationDeliveries(20), [])
  const [run, setRun] = useState<NotificationRunResult | null>(null)
  const [running, setRunning] = useState(false)
  const reloadAll = () => {
    settings.reload()
    deliveries.reload()
  }

  const onDaemon = async (v: boolean) => {
    try {
      await patchNotificationSettings({ daemonEnabled: v })
      settings.reload()
    } catch (e) {
      toast.error(errMsg(e))
    }
  }
  const onRun = async (dryRun: boolean) => {
    setRunning(true)
    try {
      setRun(await runNotificationsCycle({ dryRun }))
      if (!dryRun) reloadAll()
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setRunning(false)
    }
  }

  const s = settings.data
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Panel className="lg:col-span-2">
        <PanelHeader
          eyebrow="daemon"
          title="Productores"
          sub="Qué avisa memex y cuándo. Cada productor tiene sus tiempos; «llm» redacta con el modelo a partir de los hechos, «plantilla» no gasta."
          right={
            s && (
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                daemon {s.daemonEnabled ? "prendido" : "apagado"} · {s.timezone}
                <Switch
                  checked={s.daemonEnabled}
                  onCheckedChange={(v) => void onDaemon(v)}
                  aria-label="Daemon de notificaciones"
                />
              </label>
            )
          }
        />
        {settings.error ? (
          <PanelBody><ErrorState detail={settings.error} onRetry={settings.reload} /></PanelBody>
        ) : !s ? (
          <PanelBody className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" /> Cargando…
          </PanelBody>
        ) : (
          <ul className="divide-y divide-border">
            {s.producers.map((p) => (
              <ProducerRow key={`${p.name}:${p.fromDb}:${JSON.stringify(p.config)}`} p={p} onChanged={settings.reload} />
            ))}
          </ul>
        )}
        <div className="flex items-center gap-2 border-t px-4 py-3">
          <Button variant="outline" size="sm" className="h-8 text-xs" disabled={running} onClick={() => void onRun(true)}>
            <Play className="size-3" /> Simular ciclo
          </Button>
          <Button size="sm" className="h-8 text-xs" disabled={running} onClick={() => void onRun(false)}>
            {running ? <Loader2 className="size-3 animate-spin" /> : <Play className="size-3" />} Correr ciclo ahora
          </Button>
          <span className="text-xs text-muted-foreground">Simular no escribe ni llama al modelo.</span>
        </div>
        {run && <PanelBody className="pt-0"><RunResult r={run} /></PanelBody>}
      </Panel>

      <Panel>
        <PanelHeader
          eyebrow="entrega"
          title="Canales"
          sub="Por dónde llegan los avisos y qué recibe cada canal (severidad mínima por audiencia). En silencio solo pasa lo crítico."
        />
        {s && s.channels.length === 0 && (
          <PanelBody className="text-xs text-muted-foreground">Sin canales: los avisos solo se ven acá.</PanelBody>
        )}
        <ul className="divide-y divide-border">
          {s?.channels.map((c) => <ChannelRow key={c.id} c={c} onChanged={settings.reload} />)}
        </ul>
        {s && <AddChannel names={s.channelNames} onAdded={settings.reload} />}
      </Panel>

      <Panel>
        <PanelHeader eyebrow="entrega" title="Últimas entregas" sub="Una fila por aviso y canal; las fallidas reintentan con tope." />
        {deliveries.error ? (
          <PanelBody><ErrorState detail={deliveries.error} onRetry={deliveries.reload} /></PanelBody>
        ) : (deliveries.data ?? []).length === 0 ? (
          <PanelBody className="text-xs text-muted-foreground">Todavía no se entregó nada.</PanelBody>
        ) : (
          <ul className="divide-y divide-border text-xs">
            {(deliveries.data ?? []).map((d) => (
              <li key={d.id} className="flex items-center gap-2 px-4 py-2">
                <span className={cn("w-14 shrink-0 font-medium", d.status === "fallida" && "text-destructive")}>
                  {d.status}
                </span>
                <span className="w-20 shrink-0 text-muted-foreground">{d.channel}</span>
                <span className="min-w-0 flex-1 truncate">{d.title}</span>
                {d.attempts > 1 && <span className="text-muted-foreground">×{d.attempts}</span>}
                <span className="shrink-0 text-muted-foreground">
                  <RelativeTime date={d.sentAt ?? d.updatedAt} />
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  )
}
