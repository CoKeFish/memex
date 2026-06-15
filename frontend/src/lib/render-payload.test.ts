import { describe, expect, it } from "vitest"
import { renderPayload } from "./render-payload"

// VECTORES ESPEJO de tests/test_processing_render.py (render Python): no hay runner
// cross-language, la paridad se fija duplicando estos vectores — cambiar uno = cambiar el otro.
describe("attachmentsManifest (vía renderPayload)", () => {
  it("línea completa con sender (pinea separadores, orden y formato exactos)", () => {
    const r = renderPayload({
      from: { name: "Ana" },
      body_text: "hola",
      attachments: [{ filename: "a.pdf", size: 2500 }],
    })
    expect(r.line).toBe("Ana: hola\n[Adjuntos: a.pdf (3 KB)]")
  })

  it("duplicados se listan ambos, sin dedup (caso real inbox 1357)", () => {
    const r = renderPayload({
      subject: "ATUNALIPA ABRIL",
      body_text: "ver adjunto",
      from: { name: "Erika" },
      attachments: [
        { filename: "CAPTURA.xlsx", size: 38738, content_type: "application/vnd.x" },
        { filename: "CAPTURA.xlsx", size: 38738 },
      ],
    })
    expect(r.body).toContain("[Adjuntos: CAPTURA.xlsx (39 KB), CAPTURA.xlsx (39 KB)]")
  })

  it("redondeo half-up base 1000 con aritmética entera (no Math.round/toFixed)", () => {
    const cases: Array<[number, string]> = [
      [999, "999 B"],
      [1_000, "1 KB"],
      [2_500, "3 KB"],
      [38_738, "39 KB"],
      [999_499, "999 KB"],
      [999_500, "1000 KB"], // quirk asumido: el corte a MB es por tamaño crudo, no redondeado
      [1_000_000, "1.0 MB"],
      [1_250_000, "1.3 MB"],
      [1_950_000, "2.0 MB"],
      [10_400_000, "10.4 MB"],
    ]
    for (const [size, expected] of cases) {
      const r = renderPayload({ body_text: "x", attachments: [{ filename: "f", size }] })
      expect(r.body, `size=${size}`).toContain(`[Adjuntos: f (${expected})]`)
    }
  })

  it("fallbacks de nombre (filename → content_type → adjunto) y tamaño solo si > 0", () => {
    const r = renderPayload({
      body_text: "x",
      attachments: [
        { filename: null, content_type: "application/pdf", size: 0 },
        { filename: "", content_type: "", size: -5 },
        { size: 123 },
      ],
    })
    expect(r.body).toContain("[Adjuntos: application/pdf, adjunto, adjunto (123 B)]")
  })

  it("attachments ausente / vacío / no-lista / entradas no-dict ⇒ render previo idéntico", () => {
    const base = renderPayload({ subject: "Hola", body_text: "x", from: { name: "Ana" } })
    for (const atts of [[], "no-lista", { filename: "a" }, [42, "x", []], null]) {
      const r = renderPayload({ subject: "Hola", body_text: "x", from: { name: "Ana" }, attachments: atts })
      expect(r.line, JSON.stringify(atts)).toBe(base.line)
    }
  })

  it("posición fija: body < manifest < bloque OCR", () => {
    const r = renderPayload(
      {
        subject: "Recibo",
        body_text: "va adjunto",
        from: { name: "Tienda" },
        attachments: [{ filename: "recibo.png", size: 2048 }],
      },
      "TOTAL: $99",
    )
    const iBody = r.body.indexOf("va adjunto")
    const iManifest = r.body.indexOf("[Adjuntos: recibo.png (2 KB)]")
    const iOcr = r.body.indexOf("[Texto en imágenes adjuntas]")
    expect(iBody).toBeGreaterThanOrEqual(0)
    expect(iManifest).toBeGreaterThan(iBody)
    expect(iOcr).toBeGreaterThan(iManifest)
  })
})
