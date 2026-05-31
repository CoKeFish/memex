import {
  Bot,
  Cable,
  KeyRound,
  Lock,
  type LucideIcon,
  Plug,
  Smartphone,
  Terminal,
  Unlock,
  User,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { formatDate } from "@/lib/format"
import type { Account, ApiAccess, CliAccess, ProviderAccount, ImapOAuth } from "@/types/domain"

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <dt className="eyebrow shrink-0">{label}</dt>
      <dd className="num min-w-0 truncate text-right text-sm">{children}</dd>
    </div>
  )
}

export function IdentityPanel({ identity }: { identity: Account["identity"] }) {
  return (
    <Panel>
      <PanelHeader eyebrow="cuenta · identidad" title="Usuario" right={<User className="size-4 text-muted-foreground" />} />
      <PanelBody>
        <dl className="divide-y divide-border">
          <Row label="user_id">{identity.userId}</Row>
          <Row label="email">{identity.email}</Row>
          <Row label="display_name">{identity.displayName}</Row>
          <Row label="creado">{formatDate(identity.createdAt)}</Row>
        </dl>
        <div className="mt-3 flex items-center gap-2">
          <StatusBadge tone="ok" label="single-user" />
          <span className="text-xs text-muted-foreground">schema multi-tenant ready (todas las tablas con user_id FK)</span>
        </div>
      </PanelBody>
    </Panel>
  )
}

export function ApiAccessPanel({ api }: { api: ApiAccess }) {
  return (
    <Panel>
      <PanelHeader
        eyebrow="cuenta · acceso API"
        title="Autenticación del API"
        right={<KeyRound className="size-4 text-muted-foreground" />}
      />
      <PanelBody className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          {api.authEnforced ? (
            <StatusBadge tone="ok" label="auth enforced" />
          ) : (
            <StatusBadge tone="review" label="abierto (dev)" />
          )}
          <span className="num rounded border border-border bg-muted/30 px-2 py-0.5 text-xs">{api.tokenMasked}</span>
          <span className="text-xs text-muted-foreground">toda request → user_id={api.resolvesToUserId}</span>
        </div>
        <div className="overflow-hidden rounded-md border border-border">
          <table className="w-full text-xs">
            <tbody className="divide-y divide-border">
              {api.endpoints.map((e) => (
                <tr key={`${e.method} ${e.path}`} className="hover:bg-accent/30">
                  <td className="num w-14 px-3 py-1.5 font-medium text-muted-foreground">{e.method}</td>
                  <td className="num px-1 py-1.5">{e.path}</td>
                  <td className="px-3 py-1.5 text-right">
                    {e.auth ? (
                      <Lock className="ml-auto size-3 text-muted-foreground" />
                    ) : (
                      <Unlock className="ml-auto size-3 text-status-ok" />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-muted-foreground">
          <span className="eyebrow">falta:</span> {api.missing.join(" · ")}
        </p>
      </PanelBody>
    </Panel>
  )
}

export function CliAccessPanel({ cli }: { cli: CliAccess }) {
  return (
    <Panel>
      <PanelHeader
        eyebrow="cuenta · acceso CLI / gateway"
        title="Cliente local y plugins"
        right={<Terminal className="size-4 text-muted-foreground" />}
      />
      <PanelBody className="space-y-3">
        <dl className="divide-y divide-border">
          <Row label="gateway_url">{cli.gatewayUrl}</Row>
          <Row label="token">{cli.tokenMasked}</Row>
        </dl>
        <div>
          <div className="eyebrow mb-1.5">superficie</div>
          <ul className="space-y-1">
            {cli.surface.map((s) => (
              <li key={s} className="num rounded bg-muted/30 px-2 py-1 text-xs">{s}</li>
            ))}
          </ul>
        </div>
        <p className="text-xs text-muted-foreground">{cli.namespacing}</p>
      </PanelBody>
    </Panel>
  )
}

const TOKEN_STATE: Record<ProviderAccount["tokenState"], { tone: "ok" | "review" | "neutral"; label: string }> = {
  delta: { tone: "ok", label: "delta sync" },
  "full-resync": { tone: "review", label: "full-resync (410)" },
  never: { tone: "neutral", label: "sin sync" },
}

export function ProvidersPanel({ providers, imap }: { providers: ProviderAccount[]; imap: ImapOAuth[] }) {
  return (
    <Panel>
      <PanelHeader
        eyebrow="cuenta · proveedores externos"
        title="Cuentas conectadas"
        sub="Credenciales OAuth: la DB guarda solo el NOMBRE de la env var del token, nunca el secreto"
        right={<Plug className="size-4 text-muted-foreground" />}
      />
      <PanelBody className="space-y-2.5">
        {providers.map((p) => {
          const ts = TOKEN_STATE[p.tokenState]
          return (
            <div key={p.id} className="rounded-lg border border-border bg-background/40 p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium capitalize">
                  {p.provider} · {p.accountLabel}
                </span>
                <div className="flex items-center gap-1.5">
                  <StatusBadge tone={ts.tone} label={ts.label} />
                  <StatusBadge tone={p.writeBack ? "ok" : "neutral"} label={p.writeBack ? "write-back" : "solo lectura"} />
                </div>
              </div>
              <div className="num mt-1.5 flex flex-wrap gap-x-4 gap-y-0.5 text-[11px] text-muted-foreground">
                <span>calendar_id: {p.calendarId}</span>
                <span>sync_token: {p.syncTokenMasked ?? "—"}</span>
                {p.lastSyncAt && (
                  <span>
                    last_sync: <RelativeTime date={p.lastSyncAt} />
                  </span>
                )}
              </div>
              <div className="mt-1.5 inline-flex items-center gap-1.5 rounded border border-border bg-muted/30 px-1.5 py-0.5">
                <KeyRound className="size-3 text-muted-foreground" />
                <span className="num text-[11px]">{p.tokenPathEnv}</span>
                <span className="eyebrow">env var → archivo</span>
              </div>
            </div>
          )
        })}
        <div className="pt-1">
          <div className="eyebrow mb-1.5">IMAP OAuth (en sources.config)</div>
          {imap.map((m) => (
            <div key={m.sourceName} className="flex items-center justify-between gap-2 border-t border-border py-1.5 text-xs">
              <span>{m.sourceName}</span>
              <span className="num text-[11px] text-muted-foreground">
                {m.provider} · {m.tokenPathEnv}
              </span>
            </div>
          ))}
        </div>
      </PanelBody>
    </Panel>
  )
}

function RoadmapStep({ icon: Icon, title, detail }: { icon: LucideIcon; title: string; detail: string }) {
  return (
    <div className="flex gap-3">
      <div className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-muted/40">
        <Icon className="size-4 text-brand" />
      </div>
      <div className="min-w-0">
        <div className="text-sm font-medium">{title}</div>
        <p className="text-xs text-muted-foreground">{detail}</p>
      </div>
    </div>
  )
}

export function RoadmapPanel() {
  return (
    <Panel className={cn("lg:col-span-2")}>
      <PanelHeader
        eyebrow="cuenta · roadmap de acceso"
        title="Hacia dónde va el acceso y la ingesta"
        sub="Dirección del dueño — el dashboard consumirá del API/CLI como fuentes reales"
      />
      <PanelBody className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <RoadmapStep icon={Cable} title="API + CLI" detail="Vías de acceso principales al sistema (FastAPI + CLIs idempotentes)." />
        <RoadmapStep icon={Bot} title="Agente de IA" detail="Accederá por alguno de los dos medios (CLI o API)." />
        <RoadmapStep icon={Plug} title="Agente + canal" detail="Vía un canal, será la fuente PRINCIPAL de ingesta de datos." />
        <RoadmapStep icon={Smartphone} title="App móvil" detail="A futuro reemplazará ese canal de ingesta." />
      </PanelBody>
    </Panel>
  )
}
