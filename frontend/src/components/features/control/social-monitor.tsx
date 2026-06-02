// Redes sociales monitoreadas (Apify): una tarjeta por red (instagram/facebook/x). Cada una muestra
// el allowlist de cuentas seguidas (agregar/quitar), el estado del token de Apify (del vault) y la
// salud de la última corrida. "Traer ahora" dispara una ingesta a demanda.

import { useState } from "react"
import { Link } from "react-router-dom"
import { Download, Loader2, Plus, Trash2 } from "lucide-react"
import { toast } from "sonner"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import type { Tone } from "@/lib/status"
import { fetchAccounts, type HealthStatus, type ManagedAccount } from "@/data/accounts"
import { fetchPipeline, triggerFetch, type SourceHealthRow } from "@/data"
import {
  addFollowedAccount,
  fetchSocialSources,
  removeFollowedAccount,
  SOCIAL_PLATFORM_LABEL,
  type SocialSource,
} from "@/data/social"

const APIFY_SECRET = "apify_token"

const HEALTH_TONE: Record<HealthStatus, Tone> = {
  healthy: "ok",
  degraded: "review",
  unhealthy: "error",
  unknown: "neutral",
}

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

interface MonitorData {
  sources: SocialSource[]
  accounts: ManagedAccount[]
  pipeline: SourceHealthRow[]
}

export function SocialMonitor() {
  const { data, loading, error, reload } = useAsync<MonitorData>(async () => {
    // La salud (de /stats/pipeline) es secundaria al manejo del allowlist: si ese endpoint
    // no está disponible, igual mostramos las redes y sus cuentas (best-effort).
    const [sources, accounts, pipeline] = await Promise.all([
      fetchSocialSources(),
      fetchAccounts(),
      fetchPipeline()
        .then((p) => p.sources)
        .catch(() => [] as SourceHealthRow[]),
    ])
    return { sources, accounts, pipeline }
  }, [])

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="filtros · redes"
        title="Redes sociales monitoreadas"
        sub="Cuentas seguidas por red (vía Apify). Agregá o quitá cuentas; entran en la próxima corrida."
      />
      <PanelBody className="space-y-4">
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !data ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Cargando redes…
          </div>
        ) : !data || data.sources.length === 0 ? (
          <EmptyState
            title="Sin redes configuradas"
            hint="Creá una cuenta social y su token de Apify en Cuenta; después volvé acá para elegir a quién seguir."
          />
        ) : (
          <div className="space-y-3">
            {data.sources.map((src) => (
              <SocialCard
                key={src.id}
                src={src}
                account={data.accounts.find((a) => a.id === src.accountId) ?? null}
                health={data.pipeline.find((s) => s.sourceId === src.id) ?? null}
                onChanged={reload}
              />
            ))}
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}

function SocialCard({
  src,
  account,
  health,
  onChanged,
}: {
  src: SocialSource
  account: ManagedAccount | null
  health: SourceHealthRow | null
  onChanged: () => void
}) {
  const [handle, setHandle] = useState("")
  const [busy, setBusy] = useState(false)

  const tokenOk = account?.secrets.some((s) => s.secretName === APIFY_SECRET && s.configured) ?? false

  async function run(fn: () => Promise<unknown>, ok: string) {
    setBusy(true)
    try {
      await fn()
      toast.success(ok)
      onChanged()
    } catch (e) {
      toast.error("No se pudo aplicar", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  async function add() {
    const h = handle.trim()
    if (!h) return
    await run(async () => {
      await addFollowedAccount(src.id, h)
      setHandle("")
    }, "Cuenta agregada")
  }

  async function fetchNow() {
    setBusy(true)
    try {
      const r = await triggerFetch(src.id)
      toast.success(
        `Traído: ${r.inserted} nuevo(s), ${r.duplicates} repetido(s)`,
        { description: `${r.posted} escaneado(s) en ${r.ms_elapsed} ms` },
      )
      onChanged()
    } catch (e) {
      toast.error("No se pudo traer", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-md border border-border">
      {/* Header de la red */}
      <div className="flex flex-wrap items-center gap-2 border-b border-border bg-muted/20 px-3 py-2">
        <span className="text-sm font-medium">{SOCIAL_PLATFORM_LABEL[src.type] ?? src.type}</span>
        <span className="num text-[11px] text-muted-foreground">{src.name}</span>
        <StatusBadge tone={src.enabled ? "ok" : "neutral"} label={src.enabled ? "ON" : "OFF"} />
        {health?.lastRun && (
          <StatusBadge
            tone={health.lastRun.status === "ok" ? "ok" : health.lastRun.status === "running" ? "running" : "error"}
            label={`run ${health.lastRun.status}`}
          />
        )}
        {src.extractMedia && (
          <StatusBadge tone="neutral" label="media on" />
        )}
        <div className="ml-auto flex items-center gap-2">
          {account ? (
            <StatusBadge
              tone={tokenOk ? HEALTH_TONE[account.healthStatus] : "error"}
              label={tokenOk ? `apify ${account.healthStatus}` : "token faltante"}
            />
          ) : (
            <StatusBadge tone="error" label="sin cuenta" />
          )}
          <Button variant="outline" size="xs" disabled={busy} onClick={fetchNow}>
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Download className="size-3.5" />}
            Traer ahora
          </Button>
        </div>
      </div>

      <div className="space-y-3 p-3">
        {!tokenOk && (
          <p className="text-[11px] text-status-error">
            Falta el token de Apify de esta red.{" "}
            <Link to="/cuenta" className="underline">
              Configuralo en Cuenta
            </Link>
            .
          </p>
        )}

        {/* Agregar cuenta seguida */}
        <div className="flex items-center gap-2">
          <Input
            placeholder="handle, página o URL (p. ej. @utn.frba)"
            value={handle}
            onChange={(e) => setHandle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void add()
            }}
            className="h-8"
          />
          <Button size="sm" disabled={busy || !handle.trim()} onClick={add}>
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
            Agregar
          </Button>
        </div>

        {/* Lista de cuentas seguidas */}
        {src.accounts.length === 0 ? (
          <p className="text-[11px] text-muted-foreground">
            Sin cuentas seguidas. Agregá una arriba para empezar a monitorearla.
          </p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {src.accounts.map((a) => (
              <span
                key={a.account}
                className="inline-flex items-center gap-1 rounded-full border border-border bg-card px-2 py-0.5 text-[11px]"
              >
                <span className="num">{a.account}</span>
                <button
                  type="button"
                  disabled={busy}
                  title="Quitar"
                  onClick={() =>
                    void run(() => removeFollowedAccount(src.id, a.account), "Cuenta quitada")
                  }
                  className="text-muted-foreground hover:text-status-error disabled:opacity-50"
                >
                  <Trash2 className="size-3" />
                </button>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
