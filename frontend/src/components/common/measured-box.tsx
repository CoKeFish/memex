import { useLayoutEffect, useRef, useState, type ReactNode } from "react"

/**
 * Mide su contenedor con `useLayoutEffect` + `ResizeObserver` y entrega ancho/alto positivos al
 * render-prop. Reemplaza a `ResponsiveContainer` de Recharts, que en su primer render usa tamaño -1
 * y loguea el warning "width(-1) and height(-1) of chart should be greater than 0..." (StrictMode lo
 * amplifica al re-montar). Acá el chart solo se monta cuando ya hay un tamaño medido, sin parpadeo.
 *
 * El contenedor debe fijar sus dimensiones por CSS (p. ej. `className="h-64 w-full"`).
 */
export function MeasuredBox({
  className,
  children,
}: {
  className?: string
  children: (size: { w: number; h: number }) => ReactNode
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState<{ w: number; h: number } | null>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => {
      const { width, height } = el.getBoundingClientRect()
      if (width > 0 && height > 0) setSize({ w: width, h: height })
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return (
    <div ref={ref} className={className}>
      {size && children(size)}
    </div>
  )
}
