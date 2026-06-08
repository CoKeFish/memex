import type { LucideIcon } from "lucide-react"
import {
  Activity,
  Database,
  DollarSign,
  Image,
  LayoutDashboard,
  CalendarDays,
  Filter,
  Gauge,
  HeartPulse,
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

export interface NavGroup {
  /** Clave estable para persistir el estado de colapso. */
  id: string
  /** Encabezado del grupo. Ausente = grupo sin header (inicio / cuenta). */
  label?: string
  /** Solo los grupos con header pueden plegarse. */
  collapsible?: boolean
  items: NavItem[]
}

/**
 * Menú lateral agrupado por flujo de datos: entra (ingesta) → se procesa →
 * vive en los dominios → se observa. Inicio y Cuenta quedan sin header.
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    id: "inicio",
    items: [
      { path: "/", label: "Resumen", icon: LayoutDashboard },
      { path: "/revision", label: "Revisión", icon: ListChecks, reviewBadge: true },
    ],
  },
  {
    id: "ingesta",
    label: "Ingesta",
    collapsible: true,
    items: [
      { path: "/carga", label: "Carga / ingesta", icon: PlusCircle },
      { path: "/filtros", label: "Filtros", icon: Filter },
      { path: "/ocr", label: "Multimedia / OCR", icon: Image },
      { path: "/datos", label: "Datos", icon: Database },
    ],
  },
  {
    id: "proceso",
    label: "Proceso",
    collapsible: true,
    items: [
      { path: "/pipeline", label: "Pipeline", icon: Activity },
      { path: "/procesamiento", label: "Procesamiento", icon: SlidersHorizontal },
    ],
  },
  {
    id: "dominios",
    label: "Dominios",
    collapsible: true,
    items: [
      { path: "/calendario", label: "Calendario", icon: CalendarDays },
      { path: "/directorio", label: "Directorio", icon: Contact },
      { path: "/finanzas", label: "Finanzas", icon: Wallet },
      { path: "/bienestar", label: "Bienestar", icon: HeartPulse },
      { path: "/hackathones", label: "Hackatones", icon: Trophy },
      { path: "/grafo", label: "Grafo", icon: Share2 },
    ],
  },
  {
    id: "observabilidad",
    label: "Observabilidad",
    collapsible: true,
    items: [
      { path: "/logs", label: "Logs", icon: ScrollText },
      { path: "/metricas", label: "Métricas y costo", icon: DollarSign },
      { path: "/relevancia", label: "Relevancia", icon: Gauge },
      { path: "/calidad", label: "Calidad y precisión", icon: ShieldCheck },
    ],
  },
  {
    id: "cuenta",
    items: [{ path: "/cuenta", label: "Cuenta", icon: UserCog }],
  },
]

/** Lista plana derivada para consumidores sin grupos (paleta de comandos, títulos). */
export const NAV: NavItem[] = NAV_GROUPS.flatMap((g) => g.items)

export function navTitle(pathname: string): string {
  const exact = NAV.find((n) => n.path === pathname)
  if (exact) return exact.label
  const prefix = NAV.filter((n) => n.path !== "/").find((n) => pathname.startsWith(n.path))
  return prefix?.label ?? "Resumen"
}
