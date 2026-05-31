// Cliente HTTP mínimo contra la API de memex.
//
// Base relativa `/api` por default: en dev la mapea el proxy de Vite (ver vite.config.ts) y en
// prod la sirve el reverse proxy (Coolify/Traefik) en el mismo origen → sin CORS. `VITE_API_BASE`
// la sobrescribe si hiciera falta. `VITE_API_TOKEN` agrega el Bearer cuando MEMEX_AUTH_ENFORCED=true
// (en dev la auth está apagada y no se necesita token).

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "/api"
const TOKEN = import.meta.env.VITE_API_TOKEN as string | undefined

export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(`HTTP ${status}: ${detail}`)
    this.name = "ApiError"
    this.status = status
    this.detail = detail
  }
}

function buildHeaders(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json", ...extra }
  if (TOKEN) h["Authorization"] = `Bearer ${TOKEN}`
  return h
}

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = (await res.json()) as { detail?: unknown }
      detail = typeof body?.detail === "string" ? body.detail : JSON.stringify(body)
    } catch {
      // respuesta sin cuerpo JSON — nos quedamos con el statusText
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export async function apiGet<T>(path: string): Promise<T> {
  return handle<T>(await fetch(`${BASE}${path}`, { headers: buildHeaders() }))
}

export async function apiPost<T>(
  path: string,
  body?: unknown,
  opts?: { dryRun?: boolean },
): Promise<T> {
  const extra: Record<string, string> = {}
  if (opts?.dryRun) extra["X-Dry-Run"] = "1"
  return handle<T>(
    await fetch(`${BASE}${path}`, {
      method: "POST",
      headers: buildHeaders(extra),
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  )
}
