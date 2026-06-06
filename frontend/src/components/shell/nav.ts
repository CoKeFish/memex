import type { LucideIcon } from "lucide-react"
import {
  Activity,
  Database,
  DollarSign,
  Image,
  LayoutDashboard,
  CalendarDays,
  Filter,
  ListChecks,
  PlusCircle,
  ScrollText,
  ShieldCheck,
  Share2,
  SlidersHorizontal,
  Contact,
  Trophy,
  UserCog,
  Wallet,
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
  { path: "/calendario", label: "Calendario", icon: CalendarDays },
  { path: "/directorio", label: "Directorio", icon: Contact },
  { path: "/grafo", label: "Grafo", icon: Share2 },
  { path: "/finanzas", label: "Finanzas", icon: Wallet },
  { path: "/hackathones", label: "Hackatones", icon: Trophy },
  { path: "/metricas", label: "Métricas y costo", icon: DollarSign },
  { path: "/logs", label: "Logs", icon: ScrollText },
  { path: "/carga", label: "Carga / ingesta", icon: PlusCircle },
  { path: "/filtros", label: "Filtros", icon: Filter },
  { path: "/ocr", label: "Multimedia / OCR", icon: Image },
  { path: "/calidad", label: "Calidad y precisión", icon: ShieldCheck },
  { path: "/procesamiento", label: "Procesamiento", icon: SlidersHorizontal },
  { path: "/cuenta", label: "Cuenta", icon: UserCog },
]

export function navTitle(pathname: string): string {
  const exact = NAV.find((n) => n.path === pathname)
  if (exact) return exact.label
  const prefix = NAV.filter((n) => n.path !== "/").find((n) => pathname.startsWith(n.path))
  return prefix?.label ?? "Resumen"
}
