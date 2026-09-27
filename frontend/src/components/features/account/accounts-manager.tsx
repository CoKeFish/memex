// Gestión de cuentas y credenciales de ingestors (vault cifrado server-side).
// El valor de las credenciales se pega acá y se cifra en el backend; la UI solo ve `last4`.

import { AlertTriangle, KeyRound, Loader2, Pencil, Plug, Plus, RefreshCw, Trash2 } from "lucide-react"
import { type FormEvent, useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import { StatusBadge } from "@/components/common/led"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import {
  type AccountKind,
  createAccount,
  deleteAccount,
  deleteCredential,
  fetchAccounts,
  GOOGLE_OAUTH_SECRET,
  googleLinkState,
  HEALTH_CODE_REAUTH,
  HEALTH_LABEL,
  healthCheckAccount,
  type HealthStatus,
  type ManagedAccount,
  patchAccount,
  SECRET_LABELS,
  SECRET_NAMES,
  setCredential,
  startGoogleOAuth,
} from "@/data/accounts"
import { ApiError } from "@/lib/api"
import { formatRelative } from "@/lib/format"
import type { Tone } from "@/lib/status"
import { useAsync } from "@/lib/use-async"
import { TelegramConnect } from "./telegram-connect"
import { WhatsAppConnect } from "./whatsapp-connect"

const HEALTH_TONE: Record<HealthStatus, Tone> = {
  healthy: "ok",
  degraded: "review",
  unhealthy: "error",
  unknown: "neutral",
}

const PROVIDERS: { value: string; kind: AccountKind; label: string }[] = [
  { value: "imap", kind: "email", label: "IMAP (email)" },
  { value: "telegram", kind: "chat", label: "Telegram" },
  { value: "whatsapp", kind: "chat", label: "WhatsApp" },
  { value: "instagram", kind: "social", label: "Instagram" },
  { value: "facebook", kind: "social", label: "Facebook" },
  { value: "x", kind: "social", label: "X / Twitter" },
  { value: "notion", kind: "notes", label: "Notion (notas)" },
]

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : "Algo salió mal"
}

const inputCls =
  "rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:border-brand"
const btnCls =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs hover:bg-accent/40 disabled:opacity-50"

export function AccountsManager() {
  // `useAsync` carga al montar + en el tick de auto-refresh y expone `reload()` para los refetch
  // post-mutación; setea estado solo en callbacks async (sin set-state-in-effect).
  const { data, loading, error, reload } = useAsync(fetchAccounts)
  const accounts = data ?? []
  const [adding, setAdding] = useState(false)
  // Cuentas cuya evidencia de salud venció: se revalidan SOLAS al cargar (una vez por cuenta por
  // montaje) para que la tarjeta nunca diga «OK» por una ingesta de hace días.
  const revalidated = useRef(new Set<number>())
  useEffect(() => {
    const due = (data ?? []).filter((a) => a.healthStale && a.enabled && !revalidated.current.has(a.id))
    if (due.length === 0) return
    due.forEach((a) => revalidated.current.add(a.id))
    void Promise.allSettled(due.map((a) => healthCheckAccount(a.id))).then(() => reload())
  }, [data, reload])

  // Al volver del redirect de Google (?connected / ?oauth_error): avisar y limpiar la URL.
  // Es full-page navigation, así que el componente re-monta y la lista ya viene fresca.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    if (params.get("connected") === "google") toast.success("Google conectado")
    const oauthError = params.get("oauth_error")
    if (oauthError) toast.error(`No se pudo conectar Google: ${oauthError}`)
    if (params.has("connected") || params.has("oauth_error")) {
      window.history.replaceState({}, "", window.location.pathname)
    }
  }, [])

  return (
    <Panel className="lg:col-span-2">
      <PanelHeader
        eyebrow="cuenta · credenciales"
        title="Cuentas e ingestors"
        sub="Las credenciales se guardan CIFRADAS (vault). La UI solo muestra los últimos 4 caracteres."
        right={<Plug className="size-4 text-muted-foreground" />}
      />
      <PanelBody className="space-y-3">
        {loading ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="size-3 animate-spin" /> cargando…
          </div>
        ) : (
          <>
            {error && <p className="text-xs text-status-error">{error}</p>}
            {!error && accounts.length === 0 && (
              <p className="text-xs text-muted-foreground">
                Aún no hay cuentas. Agregá una para administrar las credenciales de tus ingestors.
              </p>
            )}
            {accounts.map((acc) => (
              <AccountCard key={acc.id} account={acc} onChange={reload} />
            ))}

            {adding ? (
              <AddAccountForm
                onCancel={() => setAdding(false)}
                onCreated={async () => {
                  setAdding(false)
                  await reload()
                }}
              />
            ) : (
              <button type="button" className={btnCls} onClick={() => setAdding(true)}>
                <Plus className="size-3" /> Agregar cuenta
              </button>
            )}
          </>
        )}
      </PanelBody>
    </Panel>
  )
}

function AddAccountForm({
  onCancel,
  onCreated,
}: {
  onCancel: () => void
  onCreated: () => void | Promise<void>
}) {
  const [alias, setAlias] = useState("")
  const [provider, setProvider] = useState(PROVIDERS[0].value)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    const kind = PROVIDERS.find((p) => p.value === provider)?.kind ?? "email"
    setBusy(true)
    try {
      await createAccount({ alias: alias.trim(), provider, kind })
      toast.success("Cuenta creada")
      await onCreated()
    } catch (err) {
      toast.error(errMsg(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2 rounded-lg border border-border bg-background/40 p-3">
      <label className="space-y-1">
        <span className="eyebrow">alias</span>
        <input
          name="account-alias"
          required
          value={alias}
          onChange={(e) => setAlias(e.target.value)}
          placeholder="mi-gmail"
          className={`${inputCls} w-40`}
        />
      </label>
      <label className="space-y-1">
        <span className="eyebrow">proveedor</span>
        <select name="provider" value={provider} onChange={(e) => setProvider(e.target.value)} className={`${inputCls} w-44`}>
          {PROVIDERS.map((p) => (
            <option key={p.value} value={p.value}>
              {p.label}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" disabled={busy} className={btnCls}>
        {busy ? <Loader2 className="size-3 animate-spin" /> : <Plus className="size-3" />} Crear
      </button>
      <button type="button" onClick={onCancel} className={btnCls}>
        Cancelar
      </button>
    </form>
  )
}

function AccountCard({ account, onChange }: { account: ManagedAccount; onChange: () => void | Promise<void> }) {
  const [checking, setChecking] = useState(false)
  // Evidencia vencida: aunque el último dato fuera «OK», no se pinta verde hasta revalidar.
  const stale = account.healthStale && account.healthStatus === "healthy"
  const tone: Tone = stale ? "review" : HEALTH_TONE[account.healthStatus]
  const statusLabel = stale ? "sin verificar" : HEALTH_LABEL[account.healthStatus]
  const configured = new Set(account.secrets.filter((s) => s.configured).map((s) => s.secretName))
  // Qué acción ofrecer respecto de Google: conectar / reconectar discreto / reconectar destacado.
  const link = googleLinkState(account)
  const hasGoogleToken = link !== null && link !== "not_connected"
  // Filas de credenciales: las que se cargan a mano + el acceso OAuth (que se ve, no se pega).
  const manual = SECRET_NAMES[account.provider] ?? []
  const expected = hasGoogleToken && !manual.includes(GOOGLE_OAUTH_SECRET)
    ? [...manual, GOOGLE_OAUTH_SECRET]
    : manual
  // Email real de la cuenta (OAuth lo deja en metadata) — solo para mostrar de qué buzón es. No es secreto.
  const rawEmail = account.metadata.email
  const accountEmail = typeof rawEmail === "string" ? rawEmail : ""

  async function connectGoogle() {
    try {
      window.location.href = await startGoogleOAuth(account.id)
    } catch (e) {
      toast.error(errMsg(e))
    }
  }

  async function runHealthCheck() {
    setChecking(true)
    try {
      const r = await healthCheckAccount(account.id)
      // El detalle también queda persistido y se muestra en la tarjeta (no solo en este toast).
      toast[r.status === "healthy" ? "success" : "error"](`${HEALTH_LABEL[r.status]}: ${r.detail}`)
      await onChange()
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setChecking(false)
    }
  }

  async function remove() {
    if (!confirm(`¿Eliminar la cuenta "${account.alias}"?`)) return
    try {
      await deleteAccount(account.id, { cascade: true })
      toast.success("Cuenta eliminada")
      await onChange()
    } catch (e) {
      toast.error(errMsg(e))
    }
  }

  return (
    <div className="rounded-lg border border-border bg-background/40 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 flex-1">
          <AliasEditor account={account} onSaved={onChange} />
          {accountEmail ? (
            <div className="truncate text-xs text-muted-foreground" title={accountEmail}>
              {accountEmail}
            </div>
          ) : null}
        </div>
        <div className="flex items-center gap-1.5">
          <span title={`estado crudo: ${account.healthStatus}`}>
            <StatusBadge tone={tone} label={statusLabel} />
          </span>
          {link === "not_connected" && (
            <button type="button" className={btnCls} onClick={connectGoogle}>
              <Plug className="size-3" /> Conectar con Google
            </button>
          )}
          {(link === "connected" || link === "unverified") && (
            <>
              <StatusBadge
                tone={link === "connected" ? "ok" : "neutral"}
                label={link === "connected" ? "Google conectado" : "token guardado · sin verificar"}
              />
              <button
                type="button"
                className={btnCls}
                onClick={connectGoogle}
                title="Volver a dar permiso en Google (si el acceso caduca o cambiás de buzón)"
              >
                Reconectar
              </button>
            </>
          )}
          {(link === "reauth" || link === "failing") && (
            <button
              type="button"
              className={`${btnCls} border-status-error text-status-error`}
              onClick={connectGoogle}
              title="El acceso guardado ya no sirve: abre el consentimiento de Google y reemplaza el token"
            >
              <AlertTriangle className="size-3" /> Reconectar con Google
            </button>
          )}
          {account.kind === "chat" && account.provider === "telegram" && (
            <TelegramConnect account={account} onDone={onChange} />
          )}
          {account.kind === "chat" && account.provider === "whatsapp" && (
            <WhatsAppConnect account={account} onDone={onChange} />
          )}
          <button
            type="button"
            className={btnCls}
            disabled={checking}
            onClick={runHealthCheck}
            title="Prueba las credenciales contra el proveedor ahora mismo"
          >
            <RefreshCw className={`size-3 ${checking ? "animate-spin" : ""}`} /> Validar
          </button>
          <button type="button" className={btnCls} onClick={remove} title="Eliminar la cuenta">
            <Trash2 className="size-3" />
          </button>
        </div>
      </div>

      <HealthLine account={account} />

      <div className="mt-2 space-y-1">
        {expected.map((name) => {
          const has = configured.has(name)
          const cred = account.secrets.find((s) => s.secretName === name)
          return (
            <div key={name} className="flex items-center justify-between gap-2 text-xs">
              <span className="inline-flex items-center gap-1.5" title={name}>
                <KeyRound className="size-3 text-muted-foreground" />
                {SECRET_LABELS[name] ?? name}
              </span>
              {!has ? (
                <span className="eyebrow text-status-pending">
                  {name === GOOGLE_OAUTH_SECRET ? "sin conectar" : "falta"}
                </span>
              ) : name === GOOGLE_OAUTH_SECRET ? (
                <span className="flex items-center gap-2">
                  <span className={link === "connected" ? "text-status-ok" : "text-status-error"}>
                    {link === "connected" ? "conectado" : "caducado"}
                  </span>
                  <button
                    type="button"
                    className="text-muted-foreground hover:text-status-error"
                    title="Quitar el acceso guardado (la cuenta vuelve a «Conectar con Google»)"
                    onClick={async () => {
                      await deleteCredential(account.id, name)
                      await onChange()
                    }}
                  >
                    <Trash2 className="size-3" />
                  </button>
                </span>
              ) : cred?.source === "env" ? (
                <span className="eyebrow text-muted-foreground">vía env</span>
              ) : (
                <span className="flex items-center gap-2">
                  <span className="num text-muted-foreground">••••{cred?.last4}</span>
                  <button
                    type="button"
                    className="text-muted-foreground hover:text-status-error"
                    onClick={async () => {
                      await deleteCredential(account.id, name)
                      await onChange()
                    }}
                  >
                    <Trash2 className="size-3" />
                  </button>
                </span>
              )}
            </div>
          )
        })}
      </div>

      <CredentialForm account={account} onSaved={onChange} />
    </div>
  )
}

/** Línea de verdad de la tarjeta: QUÉ dijo el último chequeo, CUÁNDO y de dónde sale el estado.
 * Antes el detalle vivía solo en el toast y el badge podía decir «healthy» por un run de meses. */
function HealthLine({ account }: { account: ManagedAccount }) {
  const at = account.lastHealthCheckAt
  if (account.healthBasis === "none" && !at) {
    return (
      <p className="mt-1.5 text-xs text-muted-foreground">
        Sin validar todavía · tocá «Validar» para probar las credenciales.
      </p>
    )
  }
  const bad = account.healthStatus === "unhealthy" || account.healthStatus === "degraded"
  const evidence = account.healthEvidenceAt
  const basis =
    account.healthBasis === "ingestion"
      ? `según la ingesta ${evidence ? formatRelative(evidence) : "más reciente"}`
      : at
        ? `validada ${formatRelative(at)}`
        : "validada"
  const stale = account.healthStale && account.healthStatus === "healthy"
  const label = stale ? "Sin verificar" : HEALTH_LABEL[account.healthStatus]
  const tail = stale ? " · la evidencia venció; se está revalidando" : ""
  return (
    <p className={`mt-1.5 text-xs ${bad ? "text-status-error" : "text-muted-foreground"}`}>
      <span className="font-medium">{label}</span> · {basis}{tail}
      {account.healthBasis === "check" && account.healthDetail ? ` · ${account.healthDetail}` : ""}
      {account.healthCode === HEALTH_CODE_REAUTH ? " → usá «Reconectar con Google»." : ""}
    </p>
  )
}

function AliasEditor({
  account,
  onSaved,
}: {
  account: ManagedAccount
  onSaved: () => void | Promise<void>
}) {
  const [editing, setEditing] = useState(false)
  const [alias, setAlias] = useState(account.alias)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    const next = alias.trim()
    if (!next || next === account.alias) {
      setEditing(false)
      setAlias(account.alias)
      return
    }
    setBusy(true)
    try {
      await patchAccount(account.id, { alias: next })
      toast.success("Alias actualizado")
      setEditing(false)
      await onSaved()
    } catch (err) {
      toast.error(errMsg(err))
    } finally {
      setBusy(false)
    }
  }

  if (!editing) {
    return (
      <button
        type="button"
        className="group inline-flex max-w-full items-center gap-1.5 text-sm font-medium"
        onClick={() => {
          setAlias(account.alias)
          setEditing(true)
        }}
        title="Editar alias"
      >
        <span className="truncate">
          {account.provider} · {account.alias}
        </span>
        <Pencil className="size-3 shrink-0 text-muted-foreground opacity-0 group-hover:opacity-100" />
      </button>
    )
  }

  return (
    <form onSubmit={submit} className="flex items-center gap-1.5 text-sm">
      <span className="shrink-0 text-muted-foreground">{account.provider} ·</span>
      <input
        name="account-alias-edit"
        value={alias}
        disabled={busy}
        onChange={(e) => setAlias(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setEditing(false)
            setAlias(account.alias)
          }
        }}
        className={`${inputCls} w-36 py-1`}
      />
      <button type="submit" disabled={busy} className={btnCls}>
        {busy ? <Loader2 className="size-3 animate-spin" /> : "Guardar"}
      </button>
    </form>
  )
}


function CredentialForm({
  account,
  onSaved,
}: {
  account: ManagedAccount
  onSaved: () => void | Promise<void>
}) {
  const names = SECRET_NAMES[account.provider] ?? []
  const [name, setName] = useState(names[0] ?? "")
  const [value, setValue] = useState("")
  const [busy, setBusy] = useState(false)

  if (names.length === 0) return null

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    try {
      await setCredential(account.id, name, value)
      setValue("")
      toast.success(`Credencial ${name} guardada`)
      await onSaved()
    } catch (err) {
      toast.error(errMsg(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} className="mt-2 flex flex-wrap items-end gap-2 border-t border-border pt-2">
      <select name="secret-name" value={name} onChange={(e) => setName(e.target.value)} className={`${inputCls} w-32`}>
        {names.map((n) => (
          <option key={n} value={n}>
            {SECRET_LABELS[n] ?? n}
          </option>
        ))}
      </select>
      <input
        type="password"
        name="credential-value"
        required
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="valor (se cifra)"
        autoComplete="off"
        className={`${inputCls} w-48`}
      />
      <button type="submit" disabled={busy} className={btnCls}>
        {busy ? <Loader2 className="size-3 animate-spin" /> : <Plus className="size-3" />} Guardar
      </button>
    </form>
  )
}
