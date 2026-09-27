import { describe, expect, it } from "vitest"
import { startOfDayInTz } from "./timezone"

// La lógica más delicada del fix de TZ: la medianoche de un día calendario en una zona, como instante
// UTC. El backend bucketiza por esa misma `tz`, así que estos valores deben ser exactos.
describe("startOfDayInTz", () => {
  it("convierte la medianoche local a instante UTC en zonas sin DST", () => {
    expect(startOfDayInTz(2026, 6, 1, "America/Bogota")).toBe("2026-06-01T05:00:00.000Z") // UTC-5
    expect(startOfDayInTz(2026, 6, 1, "America/Mexico_City")).toBe("2026-06-01T06:00:00.000Z") // UTC-6
    expect(startOfDayInTz(2026, 6, 1, "UTC")).toBe("2026-06-01T00:00:00.000Z")
  })

  it("respeta el DST de la zona (New York en junio = EDT, UTC-4)", () => {
    expect(startOfDayInTz(2026, 6, 1, "America/New_York")).toBe("2026-06-01T04:00:00.000Z")
    // En enero (EST, UTC-5) el offset cambia → la medianoche cae una hora más tarde en UTC.
    expect(startOfDayInTz(2026, 1, 1, "America/New_York")).toBe("2026-01-01T05:00:00.000Z")
  })
})
