// Formato de dinero de la vista de finanzas. Distinto de `formatMoney` (lib/format), que usa es-MX
// y siempre dos decimales: acá el usuario es colombiano y la moneda dominante es el peso, donde los
// centavos no existen en la práctica.
//
// Reglas: locale es-CO ($ 1.272.897 → «$1.272.897», sin el espacio que mete Intl); decimales solo si
// el monto los tiene (un peso entero se ve entero; US$29,11 conserva los centavos); una moneda que
// Intl no conoce (USDT, XLM) cae a «5 USDT» en vez de romper.

const CACHE = new Map<string, Intl.NumberFormat>()

function fmt(currency: string, decimals: number): Intl.NumberFormat | null {
  const key = `${currency}:${decimals}`
  const hit = CACHE.get(key)
  if (hit) return hit
  try {
    const f = new Intl.NumberFormat("es-CO", {
      style: "currency",
      currency,
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })
    CACHE.set(key, f)
    return f
  } catch {
    return null // moneda fuera de ISO 4217 (cripto)
  }
}

const plain = new Intl.NumberFormat("es-CO", { maximumFractionDigits: 2 })

//: Monedas donde la fracción no se usa en la vida diaria: un peso partido no existe, y al
//: sumar un periodo entero los centavos de la DB (NUMERIC(14,2)) ensuciaban la cifra
//: («$62.589.611,57»). Se redondea siempre a la unidad.
const CENTLESS = new Set(["COP", "CLP", "PYG", "JPY", "KRW", "VND", "ISK", "HUF"])

/** Decimales que merece un monto: los decide la MONEDA; en las que sí usan fracción, dos solo
 * si el monto la trae (US$29 vs US$29,11). */
function decimalsOf(amount: number, currency: string): number {
  if (CENTLESS.has(currency)) return 0
  return Number.isInteger(amount) ? 0 : 2
}

/** «$1.272.897», «US$29,11», «5 USDT». El signo negativo queda a la izquierda del símbolo. */
export function money(amount: number, currency: string): string {
  const f = fmt(currency, decimalsOf(amount, currency))
  // Sin formateador (USDT) o sin símbolo propio (XLM, que Intl escribe «XLM3,25»): el código va
  // detrás del número, como se lee una cripto.
  if (!f || currencySymbol(currency) === currency) {
    const v = CENTLESS.has(currency) ? Math.round(amount) : amount
    return `${plain.format(v)} ${currency}`
  }
  // Intl mete un espacio duro entre el símbolo y el número («$ 1.272.897»): sobra en una tabla.
  return f.format(amount).replace(/\u00a0/g, "")
}

/** Como `money` pero con el signo explícito: «+$84.000» / «−$24.139». Usa el menos tipográfico. */
export function signedMoney(amount: number, currency: string): string {
  if (amount === 0) return money(0, currency)
  const body = money(Math.abs(amount), currency)
  return `${amount > 0 ? "+" : "−"}${body}`
}

/** Solo el símbolo de la moneda («$», «US$»), para rotular un eje sin repetir el número. */
export function currencySymbol(currency: string): string {
  const f = fmt(currency, 0)
  if (!f) return currency
  return f.format(0).replace(/[\d\s\u00a0.,]/g, "") || currency
}

/** Corto para ejes y barras: «1,3 M», «845 k», «980». Sin símbolo: el eje ya dice la moneda. */
export function compactAmount(amount: number): string {
  const abs = Math.abs(amount)
  const sign = amount < 0 ? "−" : ""
  if (abs >= 1_000_000) {
    const v = abs / 1_000_000
    return `${sign}${v >= 10 ? Math.round(v) : v.toFixed(1).replace(".", ",")} M`
  }
  if (abs >= 1_000) {
    const v = abs / 1_000
    return `${sign}${v >= 10 ? Math.round(v) : v.toFixed(1).replace(".", ",")} k`
  }
  return `${sign}${Math.round(abs)}`
}
