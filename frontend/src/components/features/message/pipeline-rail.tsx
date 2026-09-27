// Riel del camino de procesamiento de UN mensaje: responde, antes de leer nada más, «¿qué le
// falta a este correo y en qué orden?». Va debajo del encabezado del mensaje porque esa pregunta
// precede al contenido; el panel «Procesamiento por fases» de abajo sigue siendo el detalle y el
// lugar de los botones (el riel solo salta hasta ahí).
//
// Lenguaje: el del resto del dashboard (etiquetas de instrumento en mono, LEDs por tono). La
// firma es el CONECTOR: la línea entre etapas dice si el flujo continuó (llena), se cortó
// (punteada) o se rompió (roja). Ningún número de paso: el orden ya lo da la línea.

import { AlertTriangle, ArrowDown, Check, CircleHelp, Minus, Pause } from "lucide-react"
import { RelativeTime } from "@/components/common/time"
import { fmtCost } from "@/components/features/message/llm-trace-runs"
import { buildPipelineSteps, nextStep, type PipelineStep, type StepState } from "@/lib/pipeline-steps"
import { cn } from "@/lib/utils"
import type { InboxRow } from "@/types/domain"

const STATE_LABEL: Record<StepState, string> = {
  done: "hecho",
  next: "siguiente",
  pending: "pendiente",
  blocked: "espera decisión",
  skipped: "no aplica",
  failed: "falló",
  unknown: "sin traza",
}

/** Color del nodo/texto por estado (tokens --status-*; `unknown`/`pending` quedan neutros). */
const STATE_TEXT: Record<StepState, string> = {
  done: "text-status-ok",
  next: "text-status-running",
  pending: "text-muted-foreground",
  blocked: "text-status-review",
  skipped: "text-status-filtered",
  failed: "text-status-error",
  unknown: "text-muted-foreground",
}

export function PipelineRail({ row }: { row: InboxRow }) {
  const steps = buildPipelineSteps(row)
  const next = nextStep(steps)
  const failed = steps.filter((s) => s.state === "failed")
  const doneCount = steps.filter((s) => s.state === "done").length
  const applicable = steps.filter((s) => s.state !== "skipped" && s.state !== "unknown").length

  return (
    <section
      aria-label="Camino de procesamiento"
      className="mt-3 rounded-md border border-border bg-muted/20 px-3 pb-3 pt-2.5"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="eyebrow">camino de procesamiento</span>
        <span className="num text-[10px] text-muted-foreground">
          {doneCount}/{applicable} etapas
        </span>
        <span className="ml-auto flex flex-wrap items-center gap-2">
          {failed.length > 0 && (
            <span className="inline-flex items-center gap-1 text-[11px] text-status-error">
              <AlertTriangle className="size-3" />
              {failed.map((f) => f.label).join(", ")} falló
            </span>
          )}
          {next ? (
            next.anchor ? (
              <a
                href={`#${next.anchor}`}
                className="inline-flex items-center gap-1 rounded-md border border-status-running/40 bg-status-running/10 px-2 py-0.5 text-[11px] font-medium text-status-running hover:bg-status-running/20"
                title={next.detail ?? undefined}
              >
                siguiente: {next.label} <ArrowDown className="size-3" />
              </a>
            ) : (
              <span
                className="inline-flex items-center gap-1 rounded-md border border-status-running/40 bg-status-running/10 px-2 py-0.5 text-[11px] font-medium text-status-running"
                title={next.detail ?? undefined}
              >
                siguiente: {next.label}
              </span>
            )
          ) : failed.length === 0 ? (
            <span className="eyebrow text-status-ok">nada pendiente</span>
          ) : null}
        </span>
      </div>

      <ol className="mt-2.5 grid grid-cols-2 gap-y-3 sm:flex sm:items-start">
        {steps.map((s, i) => (
          <StepItem key={s.key} step={s} last={i === steps.length - 1} follower={steps[i + 1]} />
        ))}
      </ol>
    </section>
  )
}

function StepItem({
  step,
  last,
  follower,
}: {
  step: PipelineStep
  last: boolean
  follower?: PipelineStep
}) {
  const text = STATE_TEXT[step.state]
  const showCost = step.costUsd != null && step.costUsd > 0
  return (
    <li
      className="flex min-w-0 flex-1 flex-col"
      aria-current={step.state === "next" ? "step" : undefined}
      title={step.detail ?? undefined}
    >
      <div className="flex items-center">
        <StepNode state={step.state} />
        {!last && <Connector from={step.state} to={follower?.state ?? "pending"} />}
      </div>
      <div className="mt-1.5 min-w-0 pr-3">
        <div className={cn("truncate text-xs font-medium", step.state === "pending" || step.state === "unknown" ? "text-muted-foreground" : "text-foreground")}>
          {step.label}
        </div>
        <div className={cn("num truncate text-[10px]", text)}>
          {step.fact ?? STATE_LABEL[step.state]}
        </div>
        {(step.at || showCost) && (
          <div className="num truncate text-[10px] text-muted-foreground">
            {step.at && <RelativeTime date={step.at} />}
            {step.at && showCost && " · "}
            {showCost && fmtCost(step.costUsd ?? 0)}
          </div>
        )}
      </div>
    </li>
  )
}

/** El nodo: lleno con check = hecho; LED pulsando = siguiente; hueco = pendiente; hueco punteado =
 * no aplica / sin traza; rojo = falló; ámbar en pausa = espera decisión. */
function StepNode({ state }: { state: StepState }) {
  const base = "grid size-6 shrink-0 place-items-center rounded-full border"
  switch (state) {
    case "done":
      return (
        <span className={cn(base, "border-status-ok/50 bg-status-ok/12 text-status-ok")}>
          <Check className="size-3.5" strokeWidth={2.5} />
        </span>
      )
    case "next":
      return (
        <span className={cn(base, "border-status-running/60 text-status-running")}>
          <span className="led led-pulse" style={{ width: 8, height: 8 }} />
        </span>
      )
    case "failed":
      return (
        <span className={cn(base, "border-status-error/60 bg-status-error/12 text-status-error")}>
          <AlertTriangle className="size-3.5" />
        </span>
      )
    case "blocked":
      return (
        <span className={cn(base, "border-status-review/60 text-status-review")}>
          <Pause className="size-3" />
        </span>
      )
    case "skipped":
      return (
        <span className={cn(base, "border-dashed border-border text-status-filtered")}>
          <Minus className="size-3" />
        </span>
      )
    case "unknown":
      return (
        <span className={cn(base, "border-dashed border-border text-muted-foreground")}>
          <CircleHelp className="size-3.5" />
        </span>
      )
    default:
      return <span className={cn(base, "border-border text-muted-foreground")} />
  }
}

/** La línea entre dos etapas cuenta si el flujo siguió: llena y verde si ambas puntas se hicieron,
 * roja si la siguiente falló, punteada si el camino se detiene (pendiente / no aplica / duda). */
function Connector({ from, to }: { from: StepState; to: StepState }) {
  const continued = from === "done" && (to === "done" || to === "unknown")
  const broke = to === "failed"
  return (
    <span
      aria-hidden
      className={cn(
        "mx-1.5 hidden h-0 flex-1 border-t-2 sm:block",
        continued && "border-solid border-status-ok/50",
        broke && "border-solid border-status-error/60",
        !continued && !broke && "border-dotted border-border",
      )}
    />
  )
}
