import { describe, expect, it } from "vitest"
import { compactAmount, currencySymbol, money, signedMoney } from "./money"

describe("money", () => {
  it("pesos enteros sin centavos y sin espacio tras el símbolo", () => {
    expect(money(1272897, "COP")).toBe("$1.272.897")
    expect(money(0, "COP")).toBe("$0")
  })

  it("conserva los centavos cuando el monto los tiene y la moneda los usa", () => {
    expect(money(29.11, "USD")).toBe("US$29,11")
    expect(money(29, "USD")).toBe("US$29")
  })

  it("en pesos nunca hay centavos, ni siquiera sumando un periodo entero", () => {
    expect(money(62589611.57, "COP")).toBe("$62.589.612")
    expect(money(778285.4, "COP")).toBe("$778.285")
  })

  it("una moneda que Intl no conoce no rompe", () => {
    expect(money(5, "USDT")).toBe("5 USDT")
    expect(money(3.25, "XLM")).toBe("3,25 XLM")
  })

  it("signedMoney antepone el signo al símbolo", () => {
    expect(signedMoney(84000, "COP")).toBe("+$84.000")
    expect(signedMoney(-24139, "COP")).toBe("−$24.139")
    expect(signedMoney(0, "COP")).toBe("$0")
  })

  it("currencySymbol", () => {
    expect(currencySymbol("COP")).toBe("$")
    expect(currencySymbol("USD")).toBe("US$")
    expect(currencySymbol("USDT")).toBe("USDT")
  })
})

describe("compactAmount", () => {
  it("millones, miles y unidades", () => {
    expect(compactAmount(1272897)).toBe("1,3 M")
    expect(compactAmount(18959176)).toBe("19 M")
    expect(compactAmount(845000)).toBe("845 k")
    expect(compactAmount(8300)).toBe("8,3 k")
    expect(compactAmount(980)).toBe("980")
    expect(compactAmount(0)).toBe("0")
  })

  it("negativos con menos tipográfico", () => {
    expect(compactAmount(-8300)).toBe("−8,3 k")
  })
})
