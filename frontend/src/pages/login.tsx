import { KeyRound } from "lucide-react"
import { type FormEvent, useState } from "react"
import { useNavigate } from "react-router-dom"
import { login, signup } from "@/data/auth"
import { ApiError } from "@/lib/api"
import { useSession } from "@/state/session"

type Mode = "login" | "signup"

export function LoginPage() {
  const navigate = useNavigate()
  const { refresh } = useSession()
  const [mode, setMode] = useState<Mode>("login")
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [displayName, setDisplayName] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      if (mode === "login") {
        await login(email.trim(), password)
      } else {
        await signup(email.trim(), password, displayName.trim() || undefined)
      }
      await refresh()
      navigate("/", { replace: true })
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Algo salió mal")
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 rounded-xl border border-border bg-card p-6 shadow-sm"
      >
        <div className="flex items-center gap-2">
          <div className="flex size-9 items-center justify-center rounded-md border border-border bg-muted/40">
            <KeyRound className="size-4 text-brand" />
          </div>
          <div>
            <div className="text-sm font-semibold">memex</div>
            <div className="text-xs text-muted-foreground">
              {mode === "login" ? "Iniciar sesión" : "Crear cuenta"}
            </div>
          </div>
        </div>

        <div className="space-y-2">
          <label className="block space-y-1">
            <span className="eyebrow">email</span>
            <input
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-brand"
            />
          </label>
          {mode === "signup" && (
            <label className="block space-y-1">
              <span className="eyebrow">nombre (opcional)</span>
              <input
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-brand"
              />
            </label>
          )}
          <label className="block space-y-1">
            <span className="eyebrow">contraseña</span>
            <input
              type="password"
              required
              minLength={mode === "signup" ? 8 : undefined}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm outline-none focus:border-brand"
            />
          </label>
        </div>

        {error && <p className="text-xs text-status-error">{error}</p>}

        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-md bg-brand px-3 py-2 text-sm font-medium text-brand-foreground disabled:opacity-50"
        >
          {busy ? "…" : mode === "login" ? "Entrar" : "Registrarme"}
        </button>

        <button
          type="button"
          onClick={() => {
            setMode(mode === "login" ? "signup" : "login")
            setError(null)
          }}
          className="w-full text-center text-xs text-muted-foreground hover:text-foreground"
        >
          {mode === "login" ? "¿No tenés cuenta? Registrate" : "¿Ya tenés cuenta? Iniciá sesión"}
        </button>
      </form>
    </div>
  )
}
