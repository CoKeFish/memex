// Gate de relevancia (correos): el portero que corre ANTES de resumen/extracción. Settings
// (encendido/modo, apagado por default) + CRUD de intereses personales — la lista de rescate
// que Opus consulta para no descartar promos que al dueño SÍ le importan.

import { useState } from "react"
import { Loader2, Plus, Trash2 } from "lucide-react"
import { toast } from "sonner"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import {
  createInterest,
  deleteInterest,
  fetchGateSettings,
  fetchInterests,
  patchGateSettings,
  patchInterest,
} from "@/data"
import type { GateMode, GateSettings, PersonalInterest } from "@/data"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"

const MODE_OPTIONS: { value: GateMode; label: string; hint: string }[] = [
  { value: "per_window", label: "Por ventana", hint: "1 llamada por ventana (más barato)" },
  { value: "per_message", label: "Por correo", hint: "1 llamada por correo (experimento)" },
]

function errMsg(e: unknown): string {
  return e instanceof ApiError ? String(e.detail) : e instanceof Error ? e.message : String(e)
}

export function RelevanceGateManager() {
  const settings = useAsync<GateSettings>(() => fetchGateSettings(), [])
  const interests = useAsync<PersonalInterest[]>(() => fetchInterests(), [])
  const [busy, setBusy] = useState(false)
  const [text, setText] = useState("")

  async function mutate(fn: () => Promise<void>, ok: string, reload: () => void) {
    setBusy(true)
    try {
      await fn()
      toast.success(ok)
      reload()
    } catch (e) {
      toast.error("No se pudo aplicar", { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  const s = settings.data

  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="filtros · gate de relevancia"
        title="Intereses personales (correos)"
        sub="portero previo a resumen/extracción: juzga cada correo contra tus intereses con Opus; no relevante = no se procesa (queda en /datos); duda = cola de revisión"
      />
      <PanelBody className="space-y-4">
        {/* Settings */}
        {settings.error ? (
          <ErrorState detail={settings.error} onRetry={settings.reload} />
        ) : !s ? (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Cargando settings…
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-md border border-border bg-muted/20 p-3">
            <label className="flex items-center gap-2 text-sm">
              <Switch
                checked={s.enabled}
                disabled={busy}
                onCheckedChange={(v) =>
                  void mutate(
                    async () => {
                      await patchGateSettings({ enabled: v })
                    },
                    v ? "Gate ENCENDIDO (paga Opus por correo nuevo)" : "Gate apagado",
                    settings.reload,
                  )
                }
              />
              <span className="font-medium">{s.enabled ? "Encendido" : "Apagado"}</span>
            </label>
            <div className="flex items-center gap-2 text-sm">
              <span className="text-muted-foreground">Modo</span>
              <Select
                value={s.mode}
                onValueChange={(v) =>
                  void mutate(
                    async () => {
                      await patchGateSettings({ mode: v as GateMode })
                    },
                    `Modo → ${v}`,
                    settings.reload,
                  )
                }
              >
                <SelectTrigger className="h-8 w-56" disabled={busy}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {MODE_OPTIONS.map((m) => (
                    <SelectItem key={m.value} value={m.value}>
                      {m.label} — {m.hint}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <span className="num text-[11px] text-muted-foreground" title="modelo del gate">
              {s.model}
            </span>
          </div>
        )}

        {/* Alta de interés */}
        <div className="flex gap-2">
          <Input
            placeholder="nuevo interés (p. ej. «descuentos de Steam»)"
            value={text}
            onChange={(e) => setText(e.target.value)}
            className="h-8"
            onKeyDown={(e) => {
              if (e.key === "Enter" && text.trim()) {
                void mutate(
                  async () => {
                    await createInterest(text.trim())
                    setText("")
                  },
                  "Interés agregado",
                  interests.reload,
                )
              }
            }}
          />
          <Button
            size="sm"
            disabled={busy || !text.trim()}
            onClick={() =>
              void mutate(
                async () => {
                  await createInterest(text.trim())
                  setText("")
                },
                "Interés agregado",
                interests.reload,
              )
            }
          >
            {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Plus className="size-3.5" />}
            Agregar
          </Button>
        </div>

        {/* Lista de intereses */}
        {interests.error ? (
          <ErrorState detail={interests.error} onRetry={interests.reload} />
        ) : !interests.data ? (
          <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Cargando intereses…
          </div>
        ) : interests.data.length === 0 ? (
          <EmptyState
            title="Sin intereses declarados"
            hint="El gate igual deja pasar hechos personales (transacciones, eventos, trámites); los intereses rescatan publicidad que SÍ te importa."
          />
        ) : (
          <div className="divide-y divide-border rounded-md border border-border">
            {interests.data.map((it) => (
              <div key={it.id} className="flex items-center gap-3 px-3 py-2">
                <Switch
                  checked={it.enabled}
                  disabled={busy}
                  onCheckedChange={(v) =>
                    void mutate(
                      async () => {
                        await patchInterest(it.id, { enabled: v })
                      },
                      v ? "Interés activado" : "Interés pausado",
                      interests.reload,
                    )
                  }
                />
                <span
                  className={`min-w-0 flex-1 truncate text-sm ${it.enabled ? "" : "text-muted-foreground line-through"}`}
                >
                  {it.text}
                </span>
                <Button
                  variant="ghost"
                  size="icon"
                  disabled={busy}
                  onClick={() =>
                    void mutate(
                      async () => {
                        await deleteInterest(it.id)
                      },
                      "Interés borrado",
                      interests.reload,
                    )
                  }
                  title="Borrar interés"
                >
                  <Trash2 className="size-3.5 text-status-error" />
                </Button>
              </div>
            ))}
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}
