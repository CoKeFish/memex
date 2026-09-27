import { Led } from "@/components/common/led"
import { RelativeTime } from "@/components/common/time"
import { cn } from "@/lib/utils"
import type { Tone } from "@/lib/status"
import type { JourneyStep } from "@/types/domain"
import { LlmExchangeCard } from "./llm-exchange"
import { MediaOcr } from "./media-ocr"

function Highlighted({ text, quote }: { text: string; quote: string }) {
  const i = quote ? text.toLowerCase().indexOf(quote.toLowerCase()) : -1
  if (i < 0) return <span>{text}</span>
  return (
    <span>
      {text.slice(0, i)}
      <mark className="rounded bg-status-ok/20 px-0.5 text-status-ok">{text.slice(i, i + quote.length)}</mark>
      {text.slice(i + quote.length)}
    </span>
  )
}

export function JourneyTimeline({ steps }: { steps: JourneyStep[] }) {
  return (
    <ol className="space-y-3">
      {steps.map((s, i) => (
        <li key={i} className="flex gap-4">
          <div className="flex flex-col items-center pt-1.5">
            <Led tone={s.tone as Tone} pulse={s.tone === "running"} />
            {i < steps.length - 1 && <span className="mt-1 w-px flex-1 bg-border" />}
          </div>
          <div className="flex-1 pb-1">
            <div className="rounded-lg border border-border bg-card p-3.5">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">{s.title}</h3>
                <span className="shrink-0 text-[11px] text-muted-foreground">
                  <RelativeTime date={s.at} />
                </span>
              </div>
              <p className="mt-1 text-sm text-muted-foreground">{s.summary}</p>
              {s.details.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {s.details.map((d, j) => (
                    <span key={j} className="num rounded bg-muted/50 px-1.5 py-0.5 text-[11px]">
                      <span className="text-muted-foreground">{d.label}</span>{" "}
                      <span className="font-medium">{d.value}</span>
                    </span>
                  ))}
                </div>
              )}
              {s.evidence && (
                <div className="mt-2.5">
                  <div className="eyebrow mb-1">evidencia (substring del original)</div>
                  <p className={cn("rounded border border-border bg-muted/20 p-2 text-xs leading-relaxed")}>
                    <Highlighted text={s.evidence.sourceText} quote={s.evidence.quote} />
                  </p>
                </div>
              )}
              {s.media && s.media.length > 0 && <MediaOcr media={s.media} />}
              {s.llm && <LlmExchangeCard ex={s.llm} />}
            </div>
          </div>
        </li>
      ))}
    </ol>
  )
}
