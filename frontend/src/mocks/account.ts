import type { Account } from "@/types/domain"
import { NOW } from "./index"

const MIN = 60_000
const HOUR = 3_600_000
function iso(msAgo: number): string {
  return new Date(NOW.getTime() - msAgo).toISOString()
}

// Estado de cuenta/acceso (mock). Refleja el estado REAL del backend (single-user,
// Bearer compartido, gateway por plugin) con TODOS los secretos enmascarados.
export const account: Account = {
  identity: {
    userId: 1,
    email: "me@local",
    displayName: "default",
    createdAt: "2026-05-23T22:20:00Z",
  },
  api: {
    authEnforced: false,
    tokenMasked: "mxk_live_••••••••••••3f9a",
    resolvesToUserId: 1,
    endpoints: [
      { method: "GET", path: "/healthz", auth: false },
      { method: "GET", path: "/readyz", auth: false },
      { method: "GET", path: "/inbox", auth: true },
      { method: "GET", path: "/inbox/{id}", auth: true },
      { method: "GET", path: "/inbox/stats", auth: true },
      { method: "GET", path: "/sources", auth: true },
      { method: "POST", path: "/sources", auth: true },
      { method: "POST", path: "/ingest", auth: true },
      { method: "POST", path: "/ingest/batch", auth: true },
      { method: "POST", path: "/gateway/plugins/{name}/ingest", auth: true, note: "superficie de clientes externos / agente" },
    ],
    missing: ["/me", "/account", "/tokens (gestión de API keys)", "tabla user_tokens (multi-user)"],
  },
  cli: {
    gatewayUrl: "https://gateway.memex.vps/…",
    tokenMasked: "mxl_••••••••••••a17c",
    surface: [
      "POST /gateway/plugins/{name}/state",
      "PUT  /gateway/plugins/{name}/cursor",
      "POST /gateway/plugins/{name}/ingest",
    ],
    namespacing: "source resuelta por (user_id + plugin_name); token compartido entre plugins, sin identidad de agente",
  },
  providers: [
    {
      id: 1,
      provider: "google",
      accountLabel: "Personal",
      calendarId: "primary",
      lastSyncAt: iso(22 * MIN),
      syncTokenMasked: "CAES•••••••••••••gQ",
      tokenPathEnv: "GOOGLE_CALENDAR_TOKEN_PATH",
      enabled: true,
      writeBack: true,
      tokenState: "delta",
    },
    {
      id: 2,
      provider: "google",
      accountLabel: "Universidad",
      calendarId: "primary",
      lastSyncAt: iso(30 * HOUR),
      syncTokenMasked: null,
      tokenPathEnv: "GOOGLE_CALENDAR_UNI_TOKEN_PATH",
      enabled: true,
      writeBack: false,
      tokenState: "full-resync",
    },
  ],
  imap: [
    { sourceName: "Correo universitario", provider: "microsoft", tokenPathEnv: "UNI_IMAP_OAUTH_TOKEN_PATH" },
    { sourceName: "Gmail personal", provider: "google", tokenPathEnv: "GMAIL_OAUTH_TOKEN_PATH" },
  ],
}
