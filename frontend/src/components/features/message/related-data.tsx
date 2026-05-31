import { cn } from "@/lib/utils"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import type { RelatedRecord } from "@/types/domain"

export function RelatedData({ related }: { related: RelatedRecord[] }) {
  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="datos relacionados"
        title="Campos y relaciones"
        sub="Registros enlazados a este mensaje, con su cardinalidad y si el API los expone"
      />
      <PanelBody className="p-0">
        <ul className="divide-y divide-border">
          {related.map((r) => (
            <li key={r.table} className="px-4 py-2.5">
              <div className="flex items-center justify-between gap-2">
                <span className="num text-sm font-medium">{r.table}</span>
                <span className="num rounded bg-muted/60 px-1.5 py-0.5 text-[10px] text-muted-foreground">{r.cardinality}</span>
              </div>
              <div className="num mt-0.5 text-[11px] text-muted-foreground">{r.relation}</div>
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                {r.keys.map((k, j) => (
                  <span key={j} className="num rounded border border-border bg-background px-1.5 py-0.5 text-[11px]">
                    <span className="text-muted-foreground">{k.label}</span> <span className="font-medium">{k.value}</span>
                  </span>
                ))}
                <span className={cn("eyebrow ml-auto", r.exposedByApi ? "text-status-ok" : "text-muted-foreground")}>
                  {r.exposedByApi ? "API" : "solo DB"}
                </span>
              </div>
            </li>
          ))}
        </ul>
      </PanelBody>
    </Panel>
  )
}
