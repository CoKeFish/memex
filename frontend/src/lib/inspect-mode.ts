// Modo inspección de las vistas de uso diario (asuntos): apagado, la página muestra solo lo que
// sirve para actuar (qué sigue, tareas, datos, archivos); encendido, además los ids, el método y
// la confianza de cada evidencia, el historial de estados y los enlaces a los mensajes con rótulo.
// Se recuerda por navegador y se comparte entre páginas (store mínimo + evento `storage`).

import { useCallback, useSyncExternalStore } from "react"

const KEY = "memex.inspect"
const listeners = new Set<() => void>()
let memory = false

function read(): boolean {
  try {
    const v = window.localStorage.getItem(KEY)
    if (v != null) return v === "1"
  } catch {
    // sin storage (modo privado, bloqueado): queda el valor en memoria
  }
  return memory
}

function write(v: boolean): void {
  memory = v
  try {
    window.localStorage.setItem(KEY, v ? "1" : "0")
  } catch {
    // idem: solo en memoria
  }
  for (const l of listeners) l()
}

function subscribe(l: () => void): () => void {
  listeners.add(l)
  const onStorage = (e: StorageEvent): void => {
    if (e.key === KEY) l()
  }
  window.addEventListener("storage", onStorage)
  return () => {
    listeners.delete(l)
    window.removeEventListener("storage", onStorage)
  }
}

export function useInspectMode(): [boolean, (v: boolean) => void] {
  const on = useSyncExternalStore(subscribe, read, () => false)
  const set = useCallback((v: boolean) => write(v), [])
  return [on, set]
}
