// Lista virtualizada genérica (extrae el patrón de features/data/inbox-feed.tsx). Para vistas
// "Todos" que pueden crecer (conflictos / dedup): solo renderiza las filas visibles. Mide la altura
// real de cada fila (`measureElement`) → filas de alto variable sin estimaciones rígidas.

import { useRef, type ReactNode } from "react"
import { useVirtualizer } from "@tanstack/react-virtual"
import { cn } from "@/lib/utils"

export function VirtualList<T>({
  items,
  getKey,
  renderItem,
  estimateSize = 64,
  maxHeight = 420,
  className,
}: {
  items: T[]
  getKey: (item: T, index: number) => string | number
  renderItem: (item: T, index: number) => ReactNode
  estimateSize?: number
  maxHeight?: number
  className?: string
}) {
  const parentRef = useRef<HTMLDivElement>(null)
  const virt = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => estimateSize,
    getItemKey: (i) => getKey(items[i], i),
    overscan: 12,
  })
  return (
    <div ref={parentRef} className={cn("overflow-y-auto", className)} style={{ maxHeight }}>
      <div style={{ height: virt.getTotalSize(), position: "relative", width: "100%" }}>
        {virt.getVirtualItems().map((vi) => (
          <div
            key={vi.key}
            data-index={vi.index}
            ref={virt.measureElement}
            style={{
              position: "absolute",
              top: 0,
              left: 0,
              width: "100%",
              transform: `translateY(${vi.start}px)`,
            }}
          >
            {renderItem(items[vi.index], vi.index)}
          </div>
        ))}
      </div>
    </div>
  )
}
