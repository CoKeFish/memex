// Gestión de cuentas y credenciales de ingestors (vault cifrado server-side).
// El valor de las credenciales se pega acá y se cifra en el backend; la UI solo ve `last4`.

import { KeyRound, Loader2, Plug, Plus, RefreshCw, Trash2 } from "lucide-react"
import { type FormEvent, useEffect, useState } from "react"
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
  healthCheckAccount,
  type HealthStatus,
  type ManagedAccount,
  SECRET_NAMES,
  setCredential,
  startGoogleOAuth,
} from "@/data/accounts"
import { ApiError } from "@/lib/api"
import type { Tone } from "@/lib/status"
import { useAsync } from "@/lib/use-async"

const HEALTH_TONE: Record<HealthStatus, Tone> = {
  healthy: "ok",
  degraded: "review",
  unhealthy: "error",
  unknown: "neutral",
}

const PROVIDERS: { value: string; kind: AccountKind; label: string }[] = [
  { value: "imap", kind: "email", label: "IMAP (email)" },
  { value: "telegram", kind: "chat", label: "Telegram" },
  { value: "instagram", kind: "social", label: "Instagram" },
  { value: "facebook", kind: "social", label: "Facebook" },
  { value: "x", kind: "social", label: "X / Twitter" },
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
          required
          value={alias}
          onChange={(e) => setAlias(e.target.value)}
          placeholder="mi-gmail"
          className={`${inputCls} w-40`}
        />
      </label>
      <label className="space-y-1">
        <span className="eyebrow">proveedor</span>
        <select value={provider} onChange={(e) => setProvider(e.target.value)} className={`${inputCls} w-44`}>
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
  const tone = HEALTH_TONE[account.healthStatus]
  const configured = new Set(account.secrets.filter((s) => s.configured).map((s) => s.secretName))
  const expected = SECRET_NAMES[account.provider] ?? []
  const googleConnected = account.secrets.some(
    (s) => s.secretName === GOOGLE_OAUTH_SECRET && s.configured,
  )

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
      toast[r.status === "healthy" ? "success" : "error"](`${r.status}: ${r.detail}`)
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
        <span className="text-sm font-medium">
          {account.provider} · {account.alias}
        </span>
        <div className="flex items-center gap-1.5">
          <StatusBadge tone={tone} label={account.healthStatus} />
          {account.kind === "email" &&
            (googleConnected ? (
              <StatusBadge tone="ok" label="google ✓" />
            ) : (
              <button type="button" className={btnCls} onClick={connectGoogle}>
                Conectar con Google
              </button>
            ))}
          <button type="button" className={btnCls} disabled={checking} onClick={runHealthCheck}>
            <RefreshCw className={`size-3 ${checking ? "animate-spin" : ""}`} /> Validar
          </button>
          <button type="button" className={btnCls} onClick={remove}>
            <Trash2 className="size-3" />
          </button>
        </div>
      </div>

      <div className="mt-2 space-y-1">
        {expected.map((name) => {
          const has = configured.has(name)
          const cred = account.secrets.find((s) => s.secretName === name)
          return (
            <div key={name} className="flex items-center justify-between gap-2 text-xs">
              <span className="num inline-flex items-center gap-1.5">
                <KeyRound className="size-3 text-muted-foreground" />
                {name}
              </span>
              {has ? (
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
              ) : (
                <span className="eyebrow text-status-pending">falta</span>
              )}
            </div>
          )
        })}
      </div>

      <CredentialForm account={account} onSaved={onChange} />
    </div>
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
      <select value={name} onChange={(e) => setName(e.target.value)} className={`${inputCls} w-32`}>
        {names.map((n) => (
          <option key={n} value={n}>
            {n}
          </option>
        ))}
      </select>
      <input
        type="password"
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
