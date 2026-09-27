import { createContext, useContext, useEffect, useState, type ReactNode } from "react"

export type Theme = "dark" | "light" | "system"

interface ThemeCtx {
  theme: Theme
  resolved: "dark" | "light"
  setTheme: (t: Theme) => void
}

const Ctx = createContext<ThemeCtx | null>(null)
const KEY = "memex.theme"

function systemDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(
    () => (localStorage.getItem(KEY) as Theme | null) ?? "dark",
  )
  const resolved: "dark" | "light" =
    theme === "system" ? (systemDark() ? "dark" : "light") : theme

  useEffect(() => {
    document.documentElement.classList.toggle("dark", resolved === "dark")
  }, [resolved])

  useEffect(() => {
    if (theme !== "system") return
    const mq = window.matchMedia("(prefers-color-scheme: dark)")
    const handler = () => document.documentElement.classList.toggle("dark", mq.matches)
    mq.addEventListener("change", handler)
    return () => mq.removeEventListener("change", handler)
  }, [theme])

  function setTheme(t: Theme) {
    setThemeState(t)
    localStorage.setItem(KEY, t)
  }

  return <Ctx.Provider value={{ theme, resolved, setTheme }}>{children}</Ctx.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useTheme(): ThemeCtx {
  const c = useContext(Ctx)
  if (!c) throw new Error("useTheme debe usarse dentro de ThemeProvider")
  return c
}
