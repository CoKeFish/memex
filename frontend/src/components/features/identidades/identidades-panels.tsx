import { useState } from "react"
import {
  Bot,
  Building2,
  Loader2,
  Mail,
  Package,
  RefreshCw,
  Sparkles,
  Star,
  Trash2,
  UserRound,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { useAsync } from "@/lib/use-async"
import {
  createIdentityOrg,
  deleteIdentityOrg,
  fetchDetected,
  fetchIdentityOrgs,
  fetchIdentityPersons,
  fetchIdentityProviderAccounts,
  fetchIdentitySyncRuns,
  triggerIdentitySync,
  updateIdentityOrg,
  updateIdentityPerson,
  type DetectedEntry,
  type IdentityKind,
  type IdentityOrg,
  type IdentityPerson,
} from "@/data"

const ORG_KINDS: { key: IdentityKind; label: string }[] = [
  { key: "organizacion", label: "Organización" },
  { key: "producto", label: "Producto" },
  { key: "agente", label: "Agente" },
]

const KIND_ICON: Record<IdentityKind, typeof Building2> = {
  organizacion: Building2,
  producto: Package,
  agente: Bot,
}

const inputCls =
  "w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-sm outline-none " +
  "placeholder:text-muted-foreground focus:border-brand/60"

function PanelLoader({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" /> {label}
    </div>
  )
}

/** interés vs Detectada (no-interés, lo que el sistema encontró solo). */
function EstadoBadge({ interest }: { interest: boolean }) {
  return interest ? (
    <span className="inline-flex items-center gap-1 text-[11px] font-medium text-brand">
      <Star className="size-2.5 fill-current" /> interés
    </span>
  ) : (
    <span className="text-[11px] font-medium text-muted-foreground">Detectada</span>
  )
}

function PromoteButton({ busy, onClick }: { busy: boolean; onClick: () => void }) {
  return (
    <Button
      size="sm"
      variant="outline"
      disabled={busy}
      onClick={onClick}
      title="Marcar como de interés"
    >
      {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Star className="size-3.5" />}
      Promover
    </Button>
  )
}

function splitCsv(s: string): string[] {
  return s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean)
}

function fmtWhen(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—"
}

// ---- Detectadas (por revisar) -----------------------------------------------------------------

export function DetectadasPanel() {
  const [busy, setBusy] = useState<string | null>(null)
  const { data, loading, error, reload } = useAsync(() => fetchDetected(), [])
  const items = data ?? []

  function promote(e: DetectedEntry): void {
    const key = `${e.kind}:${e.id}`
    setBusy(key)
    const req =
      e.kind === "person"
        ? updateIdentityPerson(e.id, { interest: true })
        : updateIdentityOrg(e.id, { interest: true })
    req.then(reload).finally(() => setBusy(null))
  }

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="directorio · por revisar"
        title="Detectadas"
        sub="Identidades que el sistema encontró en tus mensajes — promové las que te importan"
        right={<span className="eyebrow">{items.length}</span>}
      />
      <PanelBody className="p-0">
        {error ? (
          <ErrorState detail={error} />
        ) : loading && !data ? (
          <PanelLoader label="Cargando detectadas…" />
        ) : items.length === 0 ? (
          <EmptyState
            icon={<Sparkles className="size-5" />}
            title="Nada por revisar"
            hint="Cuando el módulo detecte identidades nuevas, aparecen acá para promover."
          />
        ) : (
          <ul className="divide-y divide-border">
            {items.map((e) => {
              const Icon = e.kind === "person" ? UserRound : Building2
              const key = `${e.kind}:${e.id}`
              return (
                <li key={key} className="flex items-center gap-3 px-4 py-2.5">
                  <div className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-muted/40 text-muted-foreground">
                    <Icon className="size-3.5" />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium">{e.name}</div>
                    <div className="truncate text-xs text-muted-foreground">
                      {e.kind === "person" ? "persona" : "organización"} · {e.sub}
                    </div>
                  </div>
                  <PromoteButton busy={busy === key} onClick={() => promote(e)} />
                </li>
              )
            })}
          </ul>
        )}
      </PanelBody>
    </Panel>
  )
}

// ---- Personas ---------------------------------------------------------------------------------

function PersonRow({
  p,
  busy,
  onPromote,
}: {
  p: IdentityPerson
  busy: boolean
  onPromote: () => void
}) {
  return (
    <li className={cn("flex items-start gap-3 px-4 py-2.5", p.deleted && "opacity-50")}>
      <div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full border border-border bg-muted/40 text-muted-foreground">
        <UserRound className="size-3.5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium">{p.displayName || "(sin nombre)"}</span>
          <EstadoBadge interest={p.interest} />
          {p.deleted && <span className="text-[11px] text-status-error">borrado</span>}
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-muted-foreground">
          {p.emails[0] && (
            <span className="inline-flex items-center gap-1">
              <Mail className="size-3" /> {p.emails[0]}
            </span>
          )}
          {p.orgName && (
            <span className="inline-flex items-center gap-1">
              <Building2 className="size-3" /> {p.orgName}
              {p.role ? ` · ${p.role}` : ""}
            </span>
          )}
        </div>
      </div>
      {!p.interest && <PromoteButton busy={busy} onClick={onPromote} />}
    </li>
  )
}

export function PersonsPanel() {
  const [q, setQ] = useState("")
  const [busy, setBusy] = useState<number | null>(null)
  const { data, loading, error, reload } = useAsync(() => fetchIdentityPersons(q || undefined), [q])
  const persons = data ?? []

  function promote(id: number): void {
    setBusy(id)
    updateIdentityPerson(id, { interest: true })
      .then(reload)
      .finally(() => setBusy(null))
  }

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="directorio · personas"
        title="Personas"
        sub="Contactos + detectadas en mensajes"
        right={<span className="eyebrow">{persons.length}</span>}
      />
      <div className="border-b border-border px-4 py-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Buscar por nombre o email…"
          className={inputCls}
        />
      </div>
      <PanelBody className="p-0">
        {error ? (
          <ErrorState detail={error} />
        ) : loading && !data ? (
          <PanelLoader label="Cargando personas…" />
        ) : persons.length === 0 ? (
          <EmptyState
            icon={<UserRound className="size-5" />}
            title="Sin personas"
            hint="Sincronizá Google Contacts, o extraé identidades de tus mensajes."
          />
        ) : (
          <ul className="max-h-[480px] divide-y divide-border overflow-y-auto">
            {persons.map((p) => (
              <PersonRow key={p.id} p={p} busy={busy === p.id} onPromote={() => promote(p.id)} />
            ))}
          </ul>
        )}
      </PanelBody>
    </Panel>
  )
}

// ---- Organizaciones / productos / agentes ----------------------------------------------------

function OrgRow({
  o,
  busy,
  onPromote,
  onRemove,
}: {
  o: IdentityOrg
  busy: boolean
  onPromote: () => void
  onRemove: () => void
}) {
  const Icon = KIND_ICON[o.kind] ?? Building2
  return (
    <li className="flex items-start gap-3 px-4 py-2.5">
      <div className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-muted/40 text-muted-foreground">
        <Icon className="size-3.5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium">{o.name}</span>
          <span className="eyebrow">{o.kind}</span>
          <EstadoBadge interest={o.interest} />
        </div>
        {(o.aliases.length > 0 || o.domains.length > 0) && (
          <div className="mt-0.5 truncate text-xs text-muted-foreground">
            {[...o.aliases, ...o.domains].join(", ")}
          </div>
        )}
      </div>
      {!o.interest && <PromoteButton busy={busy} onClick={onPromote} />}
      <button
        type="button"
        onClick={onRemove}
        className="mt-1 shrink-0 rounded p-1 text-muted-foreground hover:bg-status-error/10 hover:text-status-error"
        title="Quitar del directorio"
      >
        <Trash2 className="size-3.5" />
      </button>
    </li>
  )
}

export function OrgsPanel() {
  const [name, setName] = useState("")
  const [kind, setKind] = useState<IdentityKind>("organizacion")
  const [aliases, setAliases] = useState("")
  const [domains, setDomains] = useState("")
  const [busy, setBusy] = useState(false)
  const [promoting, setPromoting] = useState<number | null>(null)
  const { data, loading, error, reload } = useAsync(() => fetchIdentityOrgs(), [])
  const orgs = data ?? []

  function doSubmit(): void {
    if (!name.trim()) return
    setBusy(true)
    createIdentityOrg({
      name: name.trim(),
      kind,
      aliases: splitCsv(aliases),
      domains: splitCsv(domains),
    })
      .then(() => {
        setName("")
        setAliases("")
        setDomains("")
        reload()
      })
      .finally(() => setBusy(false))
  }

  function promote(id: number): void {
    setPromoting(id)
    updateIdentityOrg(id, { interest: true })
      .then(reload)
      .finally(() => setPromoting(null))
  }

  function remove(id: number): void {
    void deleteIdentityOrg(id).then(reload)
  }

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="directorio · organizaciones"
        title="Organizaciones / productos"
        sub="Lo que agregás manualmente (interés) + lo detectado en mensajes"
        right={<span className="eyebrow">{orgs.length}</span>}
      />
      <form
        className="space-y-2 border-b border-border px-4 py-3"
        onSubmit={(e) => {
          e.preventDefault()
          doSubmit()
        }}
      >
        <div className="flex gap-2">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Nombre (ej. Unity)"
            className={inputCls}
          />
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as IdentityKind)}
            className={cn(inputCls, "w-40")}
          >
            {ORG_KINDS.map((k) => (
              <option key={k.key} value={k.key}>
                {k.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex gap-2">
          <input
            value={aliases}
            onChange={(e) => setAliases(e.target.value)}
            placeholder="Alias, separados por coma"
            className={inputCls}
          />
          <input
            value={domains}
            onChange={(e) => setDomains(e.target.value)}
            placeholder="Dominios (unity.com)"
            className={inputCls}
          />
        </div>
        <div className="flex justify-end">
          <Button type="submit" size="sm" disabled={busy || !name.trim()}>
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : null} Agregar a interés
          </Button>
        </div>
      </form>
      <PanelBody className="p-0">
        {error ? (
          <ErrorState detail={error} />
        ) : loading && !data ? (
          <PanelLoader label="Cargando…" />
        ) : orgs.length === 0 ? (
          <EmptyState
            icon={<Building2 className="size-5" />}
            title="Sin organizaciones"
            hint="Agregá las que te interesan (Unity, Claude…), o se irán detectando solas."
          />
        ) : (
          <ul className="max-h-[420px] divide-y divide-border overflow-y-auto">
            {orgs.map((o) => (
              <OrgRow
                key={o.id}
                o={o}
                busy={promoting === o.id}
                onPromote={() => promote(o.id)}
                onRemove={() => remove(o.id)}
              />
            ))}
          </ul>
        )}
      </PanelBody>
    </Panel>
  )
}

// ---- Sync (cuentas + corridas + trigger) -----------------------------------------------------

export function SyncPanel() {
  const accounts = useAsync(() => fetchIdentityProviderAccounts(), [])
  const runs = useAsync(() => fetchIdentitySyncRuns(), [])
  const [syncing, setSyncing] = useState<number | null>(null)

  function sync(accountId: number): void {
    setSyncing(accountId)
    triggerIdentitySync(accountId)
      .then(() => {
        runs.reload()
        accounts.reload()
      })
      .finally(() => setSyncing(null))
  }

  const accs = accounts.data ?? []
  const runList = runs.data ?? []

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="directorio · sync"
        title="Sincronización de contactos"
        sub="Google People API (token del vault del dashboard)"
      />
      <PanelBody className="space-y-3">
        {accounts.error ? (
          <ErrorState detail={accounts.error} />
        ) : accs.length === 0 ? (
          <EmptyState
            icon={<RefreshCw className="size-5" />}
            title="Sin cuentas de contactos"
            hint="Vinculá una cuenta con: memex-identidades add-account --account-id <id del vault>."
          />
        ) : (
          <ul className="divide-y divide-border">
            {accs.map((a) => (
              <li key={a.id} className="flex items-center justify-between gap-3 py-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-medium">
                    {a.provider}/{a.accountLabel}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {a.syncTokenPresent ? "delta" : "full"} · último: {fmtWhen(a.lastSyncAt)}
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={syncing === a.id}
                  onClick={() => sync(a.id)}
                >
                  {syncing === a.id ? (
                    <Loader2 className="size-3.5 animate-spin" />
                  ) : (
                    <RefreshCw className="size-3.5" />
                  )}
                  Sincronizar
                </Button>
              </li>
            ))}
          </ul>
        )}

        <div>
          <div className="eyebrow mb-1.5">Corridas recientes</div>
          {runs.error ? (
            <ErrorState detail={runs.error} />
          ) : runList.length === 0 ? (
            <p className="py-3 text-xs text-muted-foreground">Sin corridas todavía.</p>
          ) : (
            <ul className="divide-y divide-border text-xs">
              {runList.slice(0, 8).map((r) => (
                <li key={r.id} className="flex items-center justify-between gap-2 py-1.5">
                  <span
                    className={cn(r.status === "error" ? "text-status-error" : "text-foreground")}
                  >
                    {r.status}
                  </span>
                  <span className="text-muted-foreground">
                    +{r.created} ~{r.modified} −{r.deleted} ={r.unchanged}
                  </span>
                  <span className="text-muted-foreground">{fmtWhen(r.startedAt)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </PanelBody>
    </Panel>
  )
}
