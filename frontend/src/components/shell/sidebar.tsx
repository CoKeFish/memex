import { ChevronDown } from "lucide-react"
import { useEffect, useState } from "react"
import { NavLink } from "react-router-dom"
import { cn } from "@/lib/utils"
import { fetchMe } from "@/data/auth"
import { useAsync } from "@/lib/use-async"
import { useAlerts } from "@/state/alerts"
import { NAV_GROUPS, type NavItem } from "./nav"

const COLLAPSE_KEY = "memex:nav:collapsed"

function readCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSE_KEY)
    if (!raw) return new Set()
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((x): x is string => typeof x === "string"))
  } catch {
    return new Set()
  }
}

function BrandMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <rect width="32" height="32" rx="7" className="fill-card" stroke="currentColor" strokeOpacity={0.15} />
      <g stroke="currentColor" strokeOpacity={0.18} strokeWidth={1}>
        <line x1="16" y1="5" x2="16" y2="27" />
        <line x1="5" y1="16" x2="27" y2="16" />
      </g>
      <circle cx="16" cy="16" r="4.5" className="fill-brand" />
      <circle cx="16" cy="16" r="8" fill="none" className="stroke-brand" strokeOpacity={0.45} strokeWidth={1.25} />
    </svg>
  )
}

export function Brand() {
  return (
    <div className="flex items-center gap-2.5 px-4 py-4">
      <BrandMark className="size-8 text-muted-foreground" />
      <div className="leading-tight">
        <div className="font-semibold tracking-tight">memex</div>
        <div className="eyebrow">consola · debug</div>
      </div>
    </div>
  )
}

function NavRow({
  item,
  reviewCount,
  onNavigate,
}: {
  item: NavItem
  reviewCount: number
  onNavigate?: () => void
}) {
  return (
    <NavLink
      to={item.path}
      end={item.path === "/"}
      onClick={onNavigate}
      className={({ isActive }) =>
        cn(
          "group relative flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
          isActive
            ? "bg-sidebar-accent font-medium text-sidebar-accent-foreground"
            : "text-muted-foreground hover:bg-sidebar-accent/50 hover:text-foreground",
        )
      }
    >
      {({ isActive }) => (
        <>
          <span
            className={cn(
              "absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-full bg-brand transition-opacity",
              isActive ? "opacity-100" : "opacity-0",
            )}
          />
          <item.icon className={cn("size-4 shrink-0", isActive && "text-brand")} />
          <span className="truncate">{item.label}</span>
          {item.reviewBadge && reviewCount > 0 && (
            <span className="num ml-auto rounded-full bg-status-review/15 px-1.5 text-[11px] font-medium text-status-review">
              {reviewCount}
            </span>
          )}
          {item.stub && (
            <span className="eyebrow ml-auto opacity-60 group-hover:opacity-100">stub</span>
          )}
        </>
      )}
    </NavLink>
  )
}

export function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  const { reviewCount } = useAlerts()
  const [collapsed, setCollapsed] = useState<Set<string>>(readCollapsed)

  useEffect(() => {
    localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...collapsed]))
  }, [collapsed])

  function toggle(id: string) {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <nav className="flex flex-col gap-0.5 px-2 py-2">
      {NAV_GROUPS.map((group, i) => {
        const isCollapsed = group.collapsible === true && collapsed.has(group.id)
        // Grupo sin header que no es el primero (Cuenta): separador sutil.
        const standaloneDivider = !group.label && i > 0
        return (
          <div
            key={group.id}
            className={cn(
              "flex flex-col gap-0.5",
              standaloneDivider && "mt-2 border-t border-sidebar-border pt-2",
            )}
          >
            {group.label &&
              (group.collapsible ? (
                <button
                  type="button"
                  onClick={() => toggle(group.id)}
                  aria-expanded={!isCollapsed}
                  className="eyebrow flex items-center gap-1.5 px-3 pb-1 pt-3 text-muted-foreground/70 transition-colors hover:text-foreground"
                >
                  <ChevronDown
                    className={cn("size-3 shrink-0 transition-transform", isCollapsed && "-rotate-90")}
                  />
                  <span>{group.label}</span>
                </button>
              ) : (
                <div className="eyebrow px-3 pb-1 pt-3">{group.label}</div>
              ))}
            {!isCollapsed &&
              group.items.map((item) => (
                <NavRow key={item.path} item={item} reviewCount={reviewCount} onNavigate={onNavigate} />
              ))}
          </div>
        )
      })}
    </nav>
  )
}

export function Sidebar() {
  const { data: me } = useAsync(fetchMe, [])
  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-sidebar-border bg-sidebar md:flex">
      <Brand />
      <div className="border-t border-sidebar-border" />
      <div className="flex-1 overflow-y-auto">
        <SidebarNav />
      </div>
      <div className="border-t border-sidebar-border px-4 py-3">
        <div className="eyebrow mb-1">single-user</div>
        <p className="truncate text-xs text-muted-foreground">
          {me?.displayName ?? me?.email ?? "local"}
        </p>
      </div>
    </aside>
  )
}
