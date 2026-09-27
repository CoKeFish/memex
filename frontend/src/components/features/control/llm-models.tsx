// Panel de /procesamiento: proveedor+modelo LLM por operación (registry general). Lee/escribe
// /llm/consumers (la fábrica `build_llm_client` del backend). Agrupado POR OPERACIÓN: las que
// usan varios consumers (calendario, identidades) muestran una sub-fila por paso. El gate de
// relevancia y el OCR usan sistemas aparte (nota al pie). Calca ModulesTogglePanel + el <Select>
// del gate (relevance-gate-manager.tsx).

import { useState } from "react"
import { AlertTriangle, Info, Loader2 } from "lucide-react"
import { toast } from "sonner"
import { ApiError } from "@/lib/api"
import { useAsync } from "@/lib/use-async"
import { Badge } from "@/components/ui/badge"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { CapBadge } from "@/components/common/cap-badge"
import { ErrorState } from "@/components/common/data-state"
import {
  type EffectiveSource,
  effectiveConfig,
  effectiveSource,
  fetchLlmConsumers,
  LLM_DEFAULT_CONSUMER,
  LLM_OPERATIONS,
  type LlmConsumerConfig,
  type LlmConsumers,
  type LlmProvider,
  MODELS_BY_PROVIDER,
  patchLlmConsumer,
  PRICED_MODELS,
} from "@/data"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? String(e.detail) : e instanceof Error ? e.message : String(e)
}

const PROVIDER_LABEL: Record<LlmProvider, string> = {
  deepseek: "DeepSeek",
  anthropic: "Anthropic",
  codex: "Codex",
}

const SOURCE_LABEL: Record<EffectiveSource, string> = {
  own: "propia",
  default: "hereda default",
  hardcode: "DeepSeek (base)",
}
const SOURCE_TITLE: Record<EffectiveSource, string> = {
  own: "tiene fila propia en llm_consumer_settings",
  default: "sin fila propia: usa la fila «default»",
  hardcode: "sin fila propia ni «default»: hardcode DeepSeek (comportamiento previo a la tabla)",
}

//: Sentinelas del <Select> de modelo (no colisionan con IDs de modelo reales).
const MODEL_DEFAULT = "__default__"
const MODEL_CUSTOM = "__custom__"

/** El estado efectivo (provider+modelo+fallback) que muestra y edita una fila. */
interface RowEff {
  provider: LlmProvider
  model: string | null
  codexModel: string | null
  fallback: LlmProvider[]
}

function toRowEff(c: LlmConsumerConfig | null): RowEff {
  return c
    ? { provider: c.provider, model: c.model, codexModel: c.codexModel, fallback: c.fallback }
    : { provider: "deepseek", model: null, codexModel: null, fallback: [] }
}

// ---- Fila de UN consumer -------------------------------------------------------------------------

function ConsumerRow({
  consumerKey,
  name,
  configured,
  providers,
  busyKey,
  onCommit,
}: {
  consumerKey: string
  name: string
  configured: LlmConsumerConfig[]
  providers: LlmProvider[]
  busyKey: string | null
  onCommit: (consumer: string, next: RowEff) => void
}) {
  const [customModel, setCustomModel] = useState(false)
  const eff = toRowEff(effectiveConfig(consumerKey, configured))
  const source = effectiveSource(consumerKey, configured)
  const busy = busyKey === consumerKey

  const isCodex = eff.provider === "codex"
  const models = MODELS_BY_PROVIDER[eff.provider]
  const currentModel = isCodex ? eff.codexModel : eff.model
  const isCustom = customModel || (currentModel != null && !models.includes(currentModel))
  const selectValue = isCustom ? MODEL_CUSTOM : currentModel == null ? MODEL_DEFAULT : currentModel
  const unpriced = !isCodex && currentModel != null && !PRICED_MODELS.has(currentModel)

  function setModel(v: string | null) {
    onCommit(consumerKey, isCodex ? { ...eff, codexModel: v } : { ...eff, model: v })
  }

  function onProviderChange(p: LlmProvider) {
    setCustomModel(false)
    // El modelo del proveedor viejo no aplica al nuevo → reset a default; saca al nuevo primario
    // de la cadena de respaldo.
    onCommit(consumerKey, {
      provider: p,
      model: null,
      codexModel: null,
      fallback: eff.fallback.filter((f) => f !== p),
    })
  }

  function onModelSelect(v: string) {
    if (v === MODEL_CUSTOM) {
      setCustomModel(true)
      return
    }
    setCustomModel(false)
    setModel(v === MODEL_DEFAULT ? null : v)
  }

  function toggleFallback(fp: LlmProvider, on: boolean) {
    const set = new Set(eff.fallback)
    if (on) set.add(fp)
    else set.delete(fp)
    // Orden = prioridad: el orden de `providers` (LLM_PROVIDERS), menos el primario.
    const ordered = providers.filter((p) => p !== eff.provider && set.has(p))
    onCommit(consumerKey, { ...eff, fallback: ordered })
  }

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 py-1.5">
      <div className="flex min-w-[140px] items-baseline gap-1.5">
        <span className="text-sm">{name}</span>
        <span className="num text-[10px] text-muted-foreground/70">{consumerKey}</span>
      </div>

      <Select
        value={eff.provider}
        onValueChange={(v) => onProviderChange(v as LlmProvider)}
        disabled={busy}
      >
        <SelectTrigger className="h-7 w-32 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {providers.map((p) => (
            <SelectItem key={p} value={p} className="text-xs">
              {PROVIDER_LABEL[p]}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <div className="flex items-center gap-1.5">
        <Select value={selectValue} onValueChange={onModelSelect} disabled={busy}>
          <SelectTrigger className="h-7 w-52 text-xs" title={isCodex ? "codex_model" : "model"}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {models.map((m) => (
              <SelectItem key={m} value={m} className="num text-xs">
                {m}
              </SelectItem>
            ))}
            <SelectItem value={MODEL_DEFAULT} className="text-xs">
              (default del proveedor)
            </SelectItem>
            <SelectItem value={MODEL_CUSTOM} className="text-xs">
              custom…
            </SelectItem>
          </SelectContent>
        </Select>
        {isCustom && (
          <Input
            defaultValue={currentModel ?? ""}
            placeholder="modelo exacto"
            disabled={busy}
            className="num h-7 w-40 text-xs"
            onBlur={(e) => {
              const v = e.target.value.trim()
              if (v !== (currentModel ?? "")) setModel(v || null)
            }}
          />
        )}
      </div>

      <div
        className="flex items-center gap-2 text-[11px] text-muted-foreground"
        title="proveedores de respaldo, en orden, si el primario agota cuota / 5xx / timeout"
      >
        <span>respaldo</span>
        {providers
          .filter((p) => p !== eff.provider)
          .map((fp) => (
            <label key={fp} className="flex items-center gap-1">
              <Checkbox
                checked={eff.fallback.includes(fp)}
                disabled={busy}
                onCheckedChange={(c) => toggleFallback(fp, c === true)}
                aria-label={`respaldo ${PROVIDER_LABEL[fp]}`}
              />
              {PROVIDER_LABEL[fp]}
            </label>
          ))}
      </div>

      <Badge
        variant={source === "own" ? "secondary" : "outline"}
        className="text-[10px]"
        title={SOURCE_TITLE[source]}
      >
        {SOURCE_LABEL[source]}
      </Badge>

      {isCodex && (
        <span
          className="flex items-center gap-1 text-[10px] text-status-review"
          title="codex: agente por suscripción — ~8–10× más lento y sin métricas de tokens (costo $0 en llm_calls)"
        >
          <AlertTriangle className="size-3" /> latencia · costo no medido
        </span>
      )}
      {unpriced && (
        <span
          className="flex items-center gap-1 text-[10px] text-muted-foreground"
          title="el modelo no está en MODEL_PRICING: su costo saldrá desconocido en /métricas"
        >
          <Info className="size-3" /> sin tarifa
        </span>
      )}
    </div>
  )
}

// ---- Panel ---------------------------------------------------------------------------------------

export function LlmModelsPanel() {
  const { data, loading, error, reload } = useAsync<LlmConsumers>(() => fetchLlmConsumers(), [])
  const [busy, setBusy] = useState<string | null>(null)

  async function commit(consumer: string, next: RowEff) {
    setBusy(consumer)
    try {
      // Snapshot COMPLETO: el upsert del backend es parcial sobre la fila PROPIA del consumer (que
      // puede no existir → base DeepSeek), así que mandar todo evita heredar un primario equivocado
      // al crear la fila. `""` limpia model/codex_model al default del proveedor.
      await patchLlmConsumer(consumer, {
        provider: next.provider,
        model: next.model ?? "",
        codexModel: next.codexModel ?? "",
        fallback: next.fallback,
      })
      reload()
    } catch (e) {
      toast.error("No se pudo cambiar el modelo", { description: errMsg(e) })
    } finally {
      setBusy(null)
    }
  }

  const configured = data?.configured ?? []
  const providers = data?.providers ?? (["deepseek", "anthropic", "codex"] as LlmProvider[])

  return (
    <Panel>
      <PanelHeader
        eyebrow="procesamiento · modelos LLM"
        title="Modelos por operación"
        sub="Elegí proveedor + modelo (y respaldos) para cada operación que usa LLM; se persiste como el default de ese paso. Las operaciones con varios pasos (calendario, identidades) se configuran por paso. «hereda default» = usa la fila global de abajo; «DeepSeek (base)» = sin config, el comportamiento previo."
        right={
          <CapBadge
            level="existe"
            title="GET/PATCH /llm/consumers — persiste en llm_consumer_settings; lo lee build_llm_client por corrida"
          />
        }
      />
      <PanelBody className="space-y-2">
        {error ? (
          <ErrorState detail={error} onRetry={reload} />
        ) : loading && !data ? (
          <div className="flex items-center gap-2 px-2 py-8 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Cargando modelos…
          </div>
        ) : !data ? null : (
          <>
            {LLM_OPERATIONS.map((op) => (
              <div key={op.label} className="rounded-md border border-border p-3">
                <div className="text-sm font-medium">
                  {op.label}
                  {op.hint && (
                    <span className="ml-2 text-[11px] font-normal text-muted-foreground">
                      {op.hint}
                    </span>
                  )}
                </div>
                <div className="mt-1 divide-y divide-border/60">
                  {op.steps.map((s) => (
                    <ConsumerRow
                      key={s.key}
                      consumerKey={s.key}
                      name={op.steps.length > 1 ? s.label : "modelo"}
                      configured={configured}
                      providers={providers}
                      busyKey={busy}
                      onCommit={commit}
                    />
                  ))}
                </div>
              </div>
            ))}

            <div className="rounded-md border border-dashed border-border p-3">
              <div className="text-sm font-medium">
                Global (default)
                <span className="ml-2 text-[11px] font-normal text-muted-foreground">
                  el default del sistema para toda operación sin fila propia
                </span>
              </div>
              <div className="mt-1">
                <ConsumerRow
                  consumerKey={LLM_DEFAULT_CONSUMER}
                  name="modelo"
                  configured={configured}
                  providers={providers}
                  busyKey={busy}
                  onCommit={commit}
                />
              </div>
            </div>

            <div className="rounded-md border border-border bg-muted/20 p-3 text-[11px] text-muted-foreground">
              <div className="mb-1 flex items-center gap-1 font-medium">
                <Info className="size-3" /> Fuera de este panel
              </div>
              <ul className="ml-4 list-disc space-y-0.5">
                <li>
                  <span className="font-medium text-foreground">Gate de relevancia</span> (correos):
                  sistema propio — se configura en <span className="font-medium">/filtros</span>.
                </li>
                <li>
                  <span className="font-medium text-foreground">OCR</span> (visión): cliente aparte
                  con su propia config (OcrConfig); no pasa por este registry.
                </li>
              </ul>
            </div>
          </>
        )}
      </PanelBody>
    </Panel>
  )
}
