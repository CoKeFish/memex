import { describe, expect, it } from "vitest"
import {
  GOOGLE_OAUTH_SECRET,
  googleLinkState,
  HEALTH_CODE_REAUTH,
  HEALTH_LABEL,
  type ManagedAccount,
  SECRET_LABELS,
  SECRET_NAMES,
} from "./accounts"

function account(over: Partial<ManagedAccount> = {}): ManagedAccount {
  return {
    id: 1,
    alias: "Gmail",
    provider: "google",
    kind: "email",
    enabled: true,
    healthStatus: "unknown",
    lastHealthCheckAt: null,
    healthDetail: null,
    healthCode: null,
    healthBasis: "none",
    healthEvidenceAt: null,
    healthStale: false,
    metadata: {},
    secrets: [],
    createdAt: "2026-06-18T00:00:00Z",
    ...over,
  }
}

const token = { secretName: GOOGLE_OAUTH_SECRET, configured: true, last4: "abcd", source: "vault" as const }

describe("googleLinkState — qué acción ofrece la tarjeta", () => {
  it("sin token → conectar", () => {
    expect(googleLinkState(account())).toBe("not_connected")
  })

  it("con token y sana → conectada (reconectar queda discreto)", () => {
    expect(googleLinkState(account({ secrets: [token], healthStatus: "healthy" }))).toBe("connected")
  })

  it("con token y sin validar todavía → sin verificar (token guardado; ni verde ni alarma)", () => {
    expect(googleLinkState(account({ secrets: [token], healthStatus: "unknown" }))).toBe("unverified")
  })

  it("con token, sana pero con la evidencia vencida → sin verificar (no se pinta verde por un run viejo)", () => {
    expect(
      googleLinkState(account({ secrets: [token], healthStatus: "healthy", healthStale: true })),
    ).toBe("unverified")
  })

  it("el backend dice reauth_required → reconectar destacado, aunque el status venga de un run viejo", () => {
    expect(
      googleLinkState(
        account({ secrets: [token], healthStatus: "unhealthy", healthCode: HEALTH_CODE_REAUTH }),
      ),
    ).toBe("reauth")
  })

  it("con token y con error por otra causa → failing (detalle + reconectar secundario)", () => {
    expect(
      googleLinkState(account({ secrets: [token], healthStatus: "unhealthy", healthCode: null })),
    ).toBe("failing")
  })

  it("no aplica a cuentas que no son de email", () => {
    expect(googleLinkState(account({ kind: "chat", provider: "telegram", secrets: [token] }))).toBeNull()
  })
})

describe("rótulos", () => {
  it("toda credencial esperada tiene rótulo humano", () => {
    const names = new Set(Object.values(SECRET_NAMES).flat().concat(GOOGLE_OAUTH_SECRET))
    for (const n of names) expect(SECRET_LABELS[n], n).toBeTruthy()
  })

  it("toda salud tiene rótulo humano", () => {
    for (const s of ["healthy", "degraded", "unhealthy", "unknown"] as const) {
      expect(HEALTH_LABEL[s]).toBeTruthy()
    }
  })

  it("una cuenta conectada por OAuth (provider google) no pide contraseña a mano", () => {
    expect(SECRET_NAMES.google).not.toContain("password")
    expect(SECRET_NAMES.google).not.toContain(GOOGLE_OAUTH_SECRET)
  })
})
