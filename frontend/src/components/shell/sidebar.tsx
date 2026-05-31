import { NavLink } from "react-router-dom"
import { cn } from "@/lib/utils"
import { useAlerts } from "@/state/alerts"
import { NAV } from "./nav"

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

export function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  const { reviewCount } = useAlerts()
  return (
    <nav className="flex flex-col gap-0.5 px-2 py-2">
      {NAV.map((item) => (
        <NavLink
          key={item.path}
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
      ))}
    </nav>
  )
}

export function Sidebar() {
  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-sidebar-border bg-sidebar md:flex">
      <Brand />
      <div className="border-t border-sidebar-border" />
      <div className="flex-1 overflow-y-auto">
        <SidebarNav />
      </div>
      <div className="border-t border-sidebar-border px-4 py-3">
        <div className="eyebrow mb-1">single-user</div>
        <p className="text-xs text-muted-foreground">me@local · datos mock</p>
      </div>
    </aside>
  )
}
