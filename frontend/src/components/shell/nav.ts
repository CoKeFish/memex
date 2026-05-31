import type { LucideIcon } from "lucide-react"
import {
  Activity,
  Database,
  DollarSign,
  Image,
  LayoutDashboard,
  ListChecks,
  PlusCircle,
  ShieldCheck,
  SlidersHorizontal,
} from "lucide-react"

export interface NavItem {
  path: string
  label: string
  icon: LucideIcon
  /** Vista stub (categoría del catálogo aún no maquetada en esta sesión). */
  stub?: boolean
  /** Muestra el contador de "pendiente de revisión". */
  reviewBadge?: boolean
}

export const NAV: NavItem[] = [
  { path: "/", label: "Resumen", icon: LayoutDashboard },
  { path: "/pipeline", label: "Pipeline", icon: Activity },
  { path: "/revision", label: "Revisión", icon: ListChecks, reviewBadge: true },
  { path: "/datos", label: "Datos", icon: Database },
  { path: "/metricas", label: "Métricas y costo", icon: DollarSign },
  { path: "/carga", label: "Carga manual", icon: PlusCircle, stub: true },
  { path: "/ocr", label: "Multimedia / OCR", icon: Image, stub: true },
  { path: "/calidad", label: "Calidad y precisión", icon: ShieldCheck, stub: true },
  { path: "/procesamiento", label: "Procesamiento", icon: SlidersHorizontal, stub: true },
]

export function navTitle(pathname: string): string {
  const exact = NAV.find((n) => n.path === pathname)
  if (exact) return exact.label
  const prefix = NAV.filter((n) => n.path !== "/").find((n) => pathname.startsWith(n.path))
  return prefix?.label ?? "Resumen"
}
