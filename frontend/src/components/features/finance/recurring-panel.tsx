// Lo que se repite: suscripciones y cobros con cadencia que el módulo detecta solo
// (`memex-finance subs detect`). Responde «¿cuánto tengo comprometido cada mes y qué se viene?»,
// que en una app de finanzas es media pantalla y acá vivía únicamente en la consola.

import { cn } from "@/lib/utils"
import { money } from "@/lib/money"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import type { Subscription, SubscriptionCommitment } from "@/data"

function cuando(days: number | null): { text: string; late: boolean } {
  if (days === null) return { text: "sin fecha", late: false }
  if (days < 0) return { text: `hace ${-days} d`, late: true }
  if (days === 0) return { text: "hoy", late: false }
  if (days === 1) return { text: "mañana", late: false }
  return { text: `en ${days} d`, late: false }
}

export function RecurringPanel({
  items,
  commitment,
}: {
  items: Subscription[]
  commitment: SubscriptionCommitment[]
}) {
  if (items.length === 0) return null
  return (
    <Panel>
      <PanelHeader
        eyebrow="se repite"
        title={
          commitment.length > 0
            ? commitment.map((c) => `${money(c.monthly, c.currency)} al mes`).join(" · ")
            : `${items.length} cobros con cadencia`
        }
        sub="Suscripciones detectadas por sus cobros: lo comprometido por mes y la próxima renovación. No se suman monedas distintas."
      />
      <PanelBody className="p-0">
        <ul className="divide-y divide-border/60">
          {items.map((s) => {
            const w = cuando(s.daysUntil)
            return (
              <li key={s.id} className="flex items-center gap-3 px-4 py-2 text-sm">
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{s.provider}</span>
                  <span className="block truncate text-[11px] text-muted-foreground">
                    {s.cadenceHuman || "cadencia por confirmar"}
                    {s.status === "sin_cobros" && " · sin cobros recientes"}
                  </span>
                </span>
                <span className="shrink-0 text-right">
                  <span className="num block text-sm">
                    {s.amountTypical === null ? "—" : money(s.amountTypical, s.currency)}
                    {s.amountVaries && (
                      <span className="ml-1 text-[11px] text-muted-foreground" title="El monto cambia entre cobros">
                        aprox.
                      </span>
                    )}
                  </span>
                  <span
                    className={cn(
                      "num block text-[11px]",
                      w.late ? "text-status-error" : "text-muted-foreground",
                    )}
                    title={s.nextRenewalOn ? `Próxima renovación: ${s.nextRenewalOn}` : undefined}
                  >
                    {w.text}
                  </span>
                </span>
              </li>
            )
          })}
        </ul>
      </PanelBody>
    </Panel>
  )
}
