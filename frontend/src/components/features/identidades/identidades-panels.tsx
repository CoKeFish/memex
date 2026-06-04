import { useState } from "react"
import {
  Building2,
  Check,
  GitMerge,
  Loader2,
  Plus,
  RefreshCw,
  Star,
  Trash2,
  UserRound,
  X,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { useAsync } from "@/lib/use-async"
import {
  addIdentifier,
  addSite,
  confirmMergeCandidate,
  createIdentity,
  deleteIdentifier,
  deleteIdentity,
  deleteSite,
  fetchIdentities,
  fetchIdentity,
  fetchIdentityProviderAccounts,
  fetchIdentitySyncRuns,
  fetchMergeCandidates,
  rejectMergeCandidate,
  triggerIdentitySync,
  updateIdentity,
  type Identity,
  type IdentityIdentifier,
  type IdentityKind,
} from "@/data"

const inputCls =
  "w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-sm outline-none " +
  "placeholder:text-muted-foreground focus:border-brand/60"

const KIND_ICON: Record<IdentityKind, typeof Building2> = {
  persona: UserRound,
  organizacion: Building2,
}
const IDENTIFIER_KINDS = ["email", "phone", "handle", "domain", "url"] as const

function PanelLoader({ label }: { label: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" /> {label}
    </div>
  )
}

function EstadoBadge({ interest }: { interest: boolean }) {
  return interest ? (
    <span className="inline-flex items-center gap-1 text-[11px] font-medium text-brand">
      <Star className="size-2.5 fill-current" /> interés
    </span>
  ) : (
    <span className="text-[11px] font-medium text-muted-foreground">Detectada</span>
  )
}

function fmtWhen(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—"
}

// ---- Directorio (lista unificada + filtros + alta + selección) --------------------------------

function IdentityRow({
  i,
  selected,
  busy,
  onSelect,
  onPromote,
}: {
  i: Identity
  selected: boolean
  busy: boolean
  onSelect: () => void
  onPromote: () => void
}) {
  const Icon = KIND_ICON[i.kind]
  return (
    <li
      className={cn(
        "flex items-center gap-3 px-4 py-2.5 cursor-pointer hover:bg-muted/40",
        selected && "bg-muted/60",
        i.deleted && "opacity-50",
      )}
      onClick={onSelect}
    >
      <div className="flex size-7 shrink-0 items-center justify-center rounded-md border border-border bg-muted/40 text-muted-foreground">
        <Icon className="size-3.5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-sm font-medium">{i.displayName || "(sin nombre)"}</span>
          <EstadoBadge interest={i.interest} />
        </div>
        {i.aliases.length > 0 && (
          <div className="truncate text-xs text-muted-foreground">{i.aliases.join(", ")}</div>
        )}
      </div>
      {!i.interest && (
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={(e) => {
            e.stopPropagation()
            onPromote()
          }}
          title="Marcar como de interés"
        >
          {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Star className="size-3.5" />}
          Promover
        </Button>
      )}
    </li>
  )
}

export function DirectoryPanel({
  selectedId,
  onSelect,
  refresh,
  onChanged,
}: {
  selectedId: number | null
  onSelect: (id: number) => void
  refresh: number
  onChanged: () => void
}) {
  const [q, setQ] = useState("")
  const [kind, setKind] = useState<IdentityKind | "">("")
  const [estado, setEstado] = useState<"" | "interes" | "detectada">("")
  const [busy, setBusy] = useState<number | null>(null)
  const [newName, setNewName] = useState("")
  const [newKind, setNewKind] = useState<IdentityKind>("organizacion")
  const [creating, setCreating] = useState(false)

  const interest = estado === "" ? undefined : estado === "interes"
  const { data, loading, error } = useAsync(
    () => fetchIdentities({ q: q || undefined, kind: kind || undefined, interest }),
    [q, kind, estado, refresh],
  )
  const items = data ?? []

  function promote(id: number): void {
    setBusy(id)
    updateIdentity(id, { interest: true })
      .then(onChanged)
      .finally(() => setBusy(null))
  }

  function create(): void {
    if (!newName.trim()) return
    setCreating(true)
    createIdentity({ kind: newKind, displayName: newName.trim() })
      .then((i) => {
        setNewName("")
        onChanged()
        onSelect(i.id)
      })
      .finally(() => setCreating(false))
  }

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="directorio · identidades"
        title="Directorio"
        sub="Personas y organizaciones — interés o Detectada"
        right={<span className="eyebrow">{items.length}</span>}
      />
      <div className="space-y-2 border-b border-border px-4 py-2.5">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Buscar por nombre o alias…"
          className={inputCls}
        />
        <div className="flex gap-2">
          <select
            value={kind}
            onChange={(e) => setKind(e.target.value as IdentityKind | "")}
            className={cn(inputCls, "w-1/2")}
          >
            <option value="">Todo tipo</option>
            <option value="persona">Personas</option>
            <option value="organizacion">Organizaciones</option>
          </select>
          <select
            value={estado}
            onChange={(e) => setEstado(e.target.value as "" | "interes" | "detectada")}
            className={cn(inputCls, "w-1/2")}
          >
            <option value="">Interés + Detectadas</option>
            <option value="interes">Solo interés</option>
            <option value="detectada">Solo Detectadas</option>
          </select>
        </div>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            create()
          }}
        >
          <input
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Nueva identidad (nombre)…"
            className={inputCls}
          />
          <select
            value={newKind}
            onChange={(e) => setNewKind(e.target.value as IdentityKind)}
            className={cn(inputCls, "w-32")}
          >
            <option value="organizacion">Org</option>
            <option value="persona">Persona</option>
          </select>
          <Button type="submit" size="sm" disabled={creating || !newName.trim()}>
            {creating ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
          </Button>
        </form>
      </div>
      <PanelBody className="p-0">
        {error ? (
          <ErrorState detail={error} />
        ) : loading && !data ? (
          <PanelLoader label="Cargando directorio…" />
        ) : items.length === 0 ? (
          <EmptyState
            icon={<UserRound className="size-5" />}
            title="Sin identidades"
            hint="Sincronizá Google Contacts, agregá una a mano, o se irán detectando en tus mensajes."
          />
        ) : (
          <ul className="max-h-[520px] divide-y divide-border overflow-y-auto">
            {items.map((i) => (
              <IdentityRow
                key={i.id}
                i={i}
                selected={i.id === selectedId}
                busy={busy === i.id}
                onSelect={() => onSelect(i.id)}
                onPromote={() => promote(i.id)}
              />
            ))}
          </ul>
        )}
      </PanelBody>
    </Panel>
  )
}

// ---- Detalle de una identidad (identificadores, sedes, afiliaciones, menciones, edición) ------

function IdentifierRow({
  idf,
  onDelete,
}: {
  idf: IdentityIdentifier
  onDelete: () => void
}) {
  return (
    <li className="flex items-center gap-2 py-1 text-sm">
      <span className="eyebrow w-24 shrink-0">{idf.platform}</span>
      <span className="text-[11px] text-muted-foreground">{idf.kind}</span>
      <span className="min-w-0 flex-1 truncate">{idf.value}</span>
      <button
        type="button"
        onClick={onDelete}
        className="shrink-0 rounded p-1 text-muted-foreground hover:bg-status-error/10 hover:text-status-error"
        title="Quitar identificador"
      >
        <Trash2 className="size-3.5" />
      </button>
    </li>
  )
}

export function IdentityDetailPanel({
  id,
  refresh,
  onChanged,
  onDeleted,
}: {
  id: number | null
  refresh: number
  onChanged: () => void
  onDeleted: () => void
}) {
  const { data, loading, error } = useAsync(
    () => (id == null ? Promise.resolve(null) : fetchIdentity(id)),
    [id, refresh],
  )
  const [notes, setNotes] = useState<string | null>(null)
  const [savingNotes, setSavingNotes] = useState(false)
  const [idfPlatform, setIdfPlatform] = useState("")
  const [idfKind, setIdfKind] = useState<IdentityIdentifier["kind"]>("email")
  const [idfValue, setIdfValue] = useState("")

  const detail = data

  if (id == null) {
    return (
      <Panel className="overflow-hidden">
        <PanelHeader eyebrow="directorio · detalle" title="Detalle" />
        <PanelBody>
          <EmptyState
            icon={<UserRound className="size-5" />}
            title="Elegí una identidad"
            hint="Hacé clic en una identidad del directorio para ver y editar su ficha."
          />
        </PanelBody>
      </Panel>
    )
  }
  if (error) {
    return (
      <Panel>
        <PanelBody>
          <ErrorState detail={error} />
        </PanelBody>
      </Panel>
    )
  }
  if (!detail || (loading && !data)) {
    return (
      <Panel>
        <PanelBody>
          <PanelLoader label="Cargando ficha…" />
        </PanelBody>
      </Panel>
    )
  }

  const { identity, identifiers, sites, affiliations, mentions } = detail
  const notesValue = notes ?? identity.notes
  const Icon = KIND_ICON[identity.kind]

  function saveNotes(): void {
    setSavingNotes(true)
    updateIdentity(identity.id, { notes: notesValue })
      .then(() => {
        setNotes(null)
        onChanged()
      })
      .finally(() => setSavingNotes(false))
  }

  function addIdf(): void {
    if (!idfValue.trim() || !idfPlatform.trim()) return
    void addIdentifier(identity.id, {
      platform: idfPlatform.trim(),
      kind: idfKind,
      value: idfValue.trim(),
    }).then(() => {
      setIdfValue("")
      onChanged()
    })
  }

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow={`directorio · ${identity.kind}`}
        title={identity.displayName || "(sin nombre)"}
        right={
          <button
            type="button"
            onClick={() => {
              void deleteIdentity(identity.id).then(() => {
                onChanged()
                onDeleted()
              })
            }}
            className="rounded p-1 text-muted-foreground hover:bg-status-error/10 hover:text-status-error"
            title="Eliminar identidad"
          >
            <Trash2 className="size-4" />
          </button>
        }
      />
      <PanelBody className="space-y-4">
        <div className="flex items-center gap-2">
          <Icon className="size-4 text-muted-foreground" />
          <EstadoBadge interest={identity.interest} />
          {!identity.interest && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                void updateIdentity(identity.id, { interest: true }).then(onChanged)
              }}
            >
              <Star className="size-3.5" /> Promover
            </Button>
          )}
          {identity.aliases.length > 0 && (
            <span className="truncate text-xs text-muted-foreground">
              alias: {identity.aliases.join(", ")}
            </span>
          )}
        </div>

        {/* Notas */}
        <div>
          <div className="eyebrow mb-1">Notas</div>
          <textarea
            value={notesValue}
            onChange={(e) => setNotes(e.target.value)}
            rows={2}
            placeholder="Notas que vas poniendo…"
            className={cn(inputCls, "resize-y")}
          />
          {notes !== null && notes !== identity.notes && (
            <div className="mt-1 flex justify-end">
              <Button size="sm" disabled={savingNotes} onClick={saveNotes}>
                {savingNotes ? <Loader2 className="size-3.5 animate-spin" /> : null} Guardar
              </Button>
            </div>
          )}
        </div>

        {/* Identificadores por-fuente */}
        <div>
          <div className="eyebrow mb-1">Identificadores</div>
          {identifiers.length === 0 ? (
            <p className="text-xs text-muted-foreground">Sin identificadores.</p>
          ) : (
            <ul className="divide-y divide-border">
              {identifiers.map((idf) => (
                <IdentifierRow
                  key={idf.id}
                  idf={idf}
                  onDelete={() => {
                    void deleteIdentifier(identity.id, idf.id).then(onChanged)
                  }}
                />
              ))}
            </ul>
          )}
          <form
            className="mt-2 flex gap-2"
            onSubmit={(e) => {
              e.preventDefault()
              addIdf()
            }}
          >
            <input
              value={idfPlatform}
              onChange={(e) => setIdfPlatform(e.target.value)}
              placeholder="plataforma (x, email…)"
              className={cn(inputCls, "w-1/3")}
            />
            <select
              value={idfKind}
              onChange={(e) => setIdfKind(e.target.value as IdentityIdentifier["kind"])}
              className={cn(inputCls, "w-28")}
            >
              {IDENTIFIER_KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
            <input
              value={idfValue}
              onChange={(e) => setIdfValue(e.target.value)}
              placeholder="valor"
              className={inputCls}
            />
            <Button type="submit" size="sm" variant="outline" disabled={!idfValue.trim()}>
              <Plus className="size-3.5" />
            </Button>
          </form>
        </div>

        {/* Sedes (solo orgs) */}
        {identity.kind === "organizacion" && (
          <SitesSection
            sites={sites}
            onAdd={(label, address, country) =>
              addSite(identity.id, { label, address, country }).then(onChanged)
            }
            onDelete={(siteId) => deleteSite(identity.id, siteId).then(onChanged)}
          />
        )}

        {/* Afiliaciones */}
        {affiliations.length > 0 && (
          <div>
            <div className="eyebrow mb-1">
              {identity.kind === "persona" ? "Organizaciones" : "Personas"}
            </div>
            <ul className="text-sm">
              {affiliations.map((a) => (
                <li key={a.id} className="py-0.5">
                  {a.displayName}
                  {a.role ? <span className="text-muted-foreground"> · {a.role}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Menciones recientes */}
        {mentions.length > 0 && (
          <div>
            <div className="eyebrow mb-1">Menciones recientes</div>
            <ul className="space-y-1 text-xs text-muted-foreground">
              {mentions.slice(0, 8).map((m) => (
                <li key={m.id} className="truncate">
                  <span className="text-foreground">{m.mentionedName}</span> · {m.resolutionMethod}
                  {m.evidence ? ` — "${m.evidence}"` : ""}
                </li>
              ))}
            </ul>
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}

function SitesSection({
  sites,
  onAdd,
  onDelete,
}: {
  sites: { id: number; label: string; address: string; country: string | null }[]
  onAdd: (label: string, address: string, country: string) => Promise<unknown>
  onDelete: (id: number) => Promise<unknown>
}) {
  const [label, setLabel] = useState("")
  const [address, setAddress] = useState("")
  const [country, setCountry] = useState("")

  return (
    <div>
      <div className="eyebrow mb-1">Sedes</div>
      {sites.length === 0 ? (
        <p className="text-xs text-muted-foreground">Sin sedes.</p>
      ) : (
        <ul className="divide-y divide-border">
          {sites.map((s) => (
            <li key={s.id} className="flex items-center gap-2 py-1 text-sm">
              <span className="eyebrow w-20 shrink-0">{s.label || "sede"}</span>
              <span className="min-w-0 flex-1 truncate">
                {s.address}
                {s.country ? ` · ${s.country}` : ""}
              </span>
              <button
                type="button"
                onClick={() => {
                  void onDelete(s.id)
                }}
                className="shrink-0 rounded p-1 text-muted-foreground hover:bg-status-error/10 hover:text-status-error"
              >
                <Trash2 className="size-3.5" />
              </button>
            </li>
          ))}
        </ul>
      )}
      <form
        className="mt-2 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          if (!address.trim() && !label.trim()) return
          void onAdd(label.trim(), address.trim(), country.trim()).then(() => {
            setLabel("")
            setAddress("")
            setCountry("")
          })
        }}
      >
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="etiqueta"
          className={cn(inputCls, "w-24")}
        />
        <input
          value={address}
          onChange={(e) => setAddress(e.target.value)}
          placeholder="dirección"
          className={inputCls}
        />
        <input
          value={country}
          onChange={(e) => setCountry(e.target.value)}
          placeholder="país"
          className={cn(inputCls, "w-20")}
        />
        <Button type="submit" size="sm" variant="outline">
          <Plus className="size-3.5" />
        </Button>
      </form>
    </div>
  )
}

// ---- Cola de revisión de merges (zona gris del difuso) ----------------------------------------

export function MergeReviewPanel({
  refresh,
  onChanged,
}: {
  refresh: number
  onChanged: () => void
}) {
  const { data, loading, error } = useAsync(() => fetchMergeCandidates(), [refresh])
  const [busy, setBusy] = useState<number | null>(null)
  const candidates = data ?? []

  function decide(id: number, confirm: boolean): void {
    setBusy(id)
    const req = confirm ? confirmMergeCandidate(id) : rejectMergeCandidate(id)
    req.then(onChanged).finally(() => setBusy(null))
  }

  if (!loading && candidates.length === 0 && !error) return null

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="directorio · revisión"
        title="Posibles duplicados"
        sub="Pares parecidos que el difuso no auto-fusionó — confirmá si son la misma identidad"
        right={<span className="eyebrow">{candidates.length}</span>}
      />
      <PanelBody className="p-0">
        {error ? (
          <ErrorState detail={error} />
        ) : loading && !data ? (
          <PanelLoader label="Cargando candidatos…" />
        ) : (
          <ul className="divide-y divide-border">
            {candidates.map((c) => (
              <li key={c.id} className="flex items-center gap-3 px-4 py-2.5">
                <GitMerge className="size-4 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1 text-sm">
                  <span className="font-medium">{c.aName}</span>
                  <span className="text-muted-foreground"> ~ </span>
                  <span className="font-medium">{c.bName}</span>
                  {c.score != null && (
                    <span className="ml-2 eyebrow">{c.score.toFixed(2)}</span>
                  )}
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy === c.id}
                  onClick={() => decide(c.id, true)}
                  title="Son la misma — fusionar"
                >
                  {busy === c.id ? (
                    <Loader2 className="size-3.5 animate-spin" />
                  ) : (
                    <Check className="size-3.5" />
                  )}
                  Fusionar
                </Button>
                <button
                  type="button"
                  onClick={() => decide(c.id, false)}
                  disabled={busy === c.id}
                  className="rounded p-1 text-muted-foreground hover:bg-muted"
                  title="Son distintas — descartar"
                >
                  <X className="size-4" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </PanelBody>
    </Panel>
  )
}

// ---- Sync (cuentas + corridas + trigger) ------------------------------------------------------

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
