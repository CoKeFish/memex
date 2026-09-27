// /jerarquia — inspección cómoda de la jerarquía de pertenencia y las afiliaciones, evaluadas
// contra el LIBRO de evidencia: por vínculo, cuántos correos lo sustentan, con qué confianza y por
// qué vía (llm/organizer/curator/manual/agent/sync/legacy); marcas «en disputa» (otro padre con
// soporte), «débil» (un solo correo, sin pin) y padres nombrados sin resolver. Clic en una entrada:
// sus candidatos, sus reclamos (con el correo de origen enlazado a /datos/:id), sus personas y sus
// hijos, con acciones: confirmar (pinear), elegir otro padre, quitar padre, rechazar un reclamo,
// fusionar un duplicado, derivar. Arriba: «Derivar» (recalcula desde el libro) y «Curar con codex».

import { useMemo, useState } from "react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import {
  Building2,
  Check,
  ChevronDown,
  ChevronRight,
  GitMerge,
  Network,
  Pin,
  RefreshCw,
  Sparkles,
  Users,
  X,
} from "lucide-react"
import { cn } from "@/lib/utils"
import { PageHeader } from "@/components/common/page-header"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { EmptyState, ErrorState } from "@/components/common/data-state"
import { Button } from "@/components/ui/button"
import { useAsync } from "@/lib/use-async"
import { ApiError } from "@/lib/api"
import {
  confirmParent,
  curateHierarchy,
  deriveHierarchy,
  fetchHierarchy,
  fetchIdentityClaims,
  fetchMergeCandidates,
  mergeIdentities,
  rejectClaim,
  updateIdentity,
  type CurateInput,
  type CurateResult,
  type HierarchyAffiliation,
  type HierarchyNode,
  type IdentityMergeCandidate,
  type LinkClaim,
} from "@/data"

type Filter = "all" | "disputed" | "weak" | "unresolved" | "roots" | "pinned"

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "Todas" },
  { key: "disputed", label: "En disputa" },
  { key: "weak", label: "Débiles" },
  { key: "unresolved", label: "Sin resolver" },
  { key: "roots", label: "Raíces sospechosas" },
  { key: "pinned", label: "Confirmadas" },
]

const SCOPES: { key: CurateInput["scope"]; label: string }[] = [
  { key: "tree", label: "árbol completo (holístico)" },
  { key: "all", label: "todo lo dudoso" },
  { key: "disputed", label: "disputas" },
  { key: "weak", label: "débiles" },
  { key: "unresolved", label: "nombres sin resolver" },
  { key: "candidates", label: "fusiones grises" },
  { key: "roots", label: "raíces sospechosas" },
]

const JUNK = ["noreply", "no-reply", "donotreply", "notify", "newsletter", "mailer"]

function isSuspiciousRoot(n: HierarchyNode): boolean {
  if (n.parentId != null) return false
  const name = n.displayName.toLowerCase()
  if (name.includes("@") || JUNK.some((j) => name.includes(j))) return true
  return n.createdBy === "hierarchy_llm" || (n.mentionCount === 0 && n.childrenCount > 0)
}

function matches(n: HierarchyNode, f: Filter): boolean {
  switch (f) {
    case "disputed":
      return n.disputed
    case "weak":
      return n.weak
    case "unresolved":
      return n.unresolvedNames.length > 0
    case "roots":
      return isSuspiciousRoot(n)
    case "pinned":
      return n.pinned
    default:
      return true
  }
}

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.message
  return e instanceof Error ? e.message : String(e)
}

function Flag({ kind, children }: { kind: "warn" | "error" | "ok" | "muted"; children: string }) {
  return (
    <span
      className={cn(
        "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide",
        kind === "error" && "bg-status-error/10 text-status-error",
        kind === "warn" && "bg-status-review/10 text-status-review",
        kind === "ok" && "bg-status-ok/10 text-status-ok",
        kind === "muted" && "bg-muted text-muted-foreground",
      )}
    >
      {children}
    </span>
  )
}

function LinkMarks({ n }: { n: HierarchyNode }) {
  if (n.parentId == null) return null
  return (
    <span className="flex shrink-0 items-center gap-1">
      <span className="text-[11px] text-muted-foreground">
        {n.messages === 0 ? "sin correo" : `${n.messages} ${n.messages === 1 ? "correo" : "correos"}`} ·{" "}
        {n.parentSource ?? "?"}
      </span>
      {n.pinned && <Flag kind="ok">pin</Flag>}
      {n.disputed && <Flag kind="error">disputa</Flag>}
      {n.weak && <Flag kind="warn">débil</Flag>}
    </span>
  )
}

// ---- Árbol ------------------------------------------------------------------------------------

function TreeRow({
  n,
  depth,
  childrenOf,
  affCount,
  selected,
  onSelect,
  expanded,
  onToggle,
}: {
  n: HierarchyNode
  depth: number
  childrenOf: Map<number, HierarchyNode[]>
  affCount: Map<number, number>
  selected: number | null
  onSelect: (id: number) => void
  expanded: Set<number>
  onToggle: (id: number) => void
}) {
  const kids = childrenOf.get(n.id) ?? []
  const open = expanded.has(n.id)
  const people = affCount.get(n.id) ?? 0
  return (
    <li>
      <div
        className={cn(
          "flex w-full items-center gap-1.5 py-1 pr-3 text-sm hover:bg-muted/40",
          selected === n.id && "bg-brand/10",
        )}
        style={{ paddingLeft: 8 + depth * 18 }}
      >
        {kids.length > 0 ? (
          <button
            type="button"
            onClick={() => onToggle(n.id)}
            className="shrink-0 rounded p-0.5 text-muted-foreground hover:bg-muted"
            aria-label={open ? "Plegar" : "Desplegar"}
          >
            {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
          </button>
        ) : (
          <span className="size-4 shrink-0" />
        )}
        <button
          type="button"
          onClick={() => onSelect(n.id)}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <Building2 className="size-3.5 shrink-0 text-muted-foreground" />
          <span className="truncate">{n.displayName}</span>
          {n.kind === "producto" && <Flag kind="muted">producto</Flag>}
          {n.unresolvedNames.length > 0 && <Flag kind="warn">nombre sin resolver</Flag>}
          {kids.length > 0 && (
            <span className="text-[11px] text-muted-foreground">· {kids.length} sub</span>
          )}
          {people > 0 && (
            <span className="flex items-center gap-0.5 text-[11px] text-muted-foreground">
              <Users className="size-3" /> {people}
            </span>
          )}
        </button>
        <LinkMarks n={n} />
      </div>
      {open && kids.length > 0 && (
        <ul>
          {kids.map((k) => (
            <TreeRow
              key={k.id}
              n={k}
              depth={depth + 1}
              childrenOf={childrenOf}
              affCount={affCount}
              selected={selected}
              onSelect={onSelect}
              expanded={expanded}
              onToggle={onToggle}
            />
          ))}
        </ul>
      )}
    </li>
  )
}

// ---- Detalle de un vínculo --------------------------------------------------------------------

function ClaimRow({
  c,
  self,
  busy,
  onToggle,
}: {
  c: LinkClaim
  self: number
  busy: boolean
  onToggle: (c: LinkClaim) => void
}) {
  const outgoing = c.sourceId === self
  const other = outgoing
    ? (c.targetName ?? c.asWritten ?? "?")
    : (c.sourceName ?? `#${c.sourceId}`)
  const verb = c.kind === "afiliado" ? (outgoing ? "miembro de" : "tiene como miembro a") : outgoing ? "pertenece a" : "contiene a"
  return (
    <li
      className={cn(
        "flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border/60 py-1.5 text-sm last:border-0",
        c.status === "rejected" && "opacity-50",
      )}
    >
      <span className="text-muted-foreground">{verb}</span>
      <span className="font-medium">{other}</span>
      {c.targetId == null && c.kind === "pertenece_a" && <Flag kind="warn">sin resolver</Flag>}
      {c.role && <span className="text-muted-foreground">· rol «{c.role}»</span>}
      <Flag kind="muted">{c.method}</Flag>
      <span className="num text-[11px] text-muted-foreground">{c.confidence.toFixed(2)}</span>
      {c.inboxId != null ? (
        <Link
          to={`/datos/${c.inboxId}`}
          className="truncate text-[12px] text-brand hover:underline"
          title={c.subject ?? undefined}
        >
          correo #{c.inboxId}
          {c.subject ? ` · ${c.subject}` : ""}
          {c.occurredAt ? ` · ${c.occurredAt.slice(0, 10)}` : ""}
        </Link>
      ) : (
        <span className="text-[12px] text-muted-foreground">sin correo</span>
      )}
      {c.rationale && (
        <span className="w-full text-[12px] text-muted-foreground">{c.rationale}</span>
      )}
      <button
        type="button"
        disabled={busy}
        onClick={() => onToggle(c)}
        className="ml-auto rounded px-1.5 py-0.5 text-[11px] text-muted-foreground hover:bg-muted"
        title={c.status === "rejected" ? "Reactivar reclamo" : "Rechazar reclamo"}
      >
        {c.status === "rejected" ? "reactivar" : "rechazar"}
      </button>
    </li>
  )
}

function NodeDetail({
  n,
  nodes,
  affiliations,
  candidates,
  version,
  onChanged,
  onSelect,
}: {
  n: HierarchyNode
  nodes: HierarchyNode[]
  affiliations: HierarchyAffiliation[]
  candidates: IdentityMergeCandidate[]
  version: number
  onChanged: () => void
  onSelect: (id: number) => void
}) {
  const [includeRejected, setIncludeRejected] = useState(false)
  const [busy, setBusy] = useState(false)
  const claims = useAsync(
    () => fetchIdentityClaims(n.id, includeRejected),
    [n.id, includeRejected, version],
  )
  const children = nodes.filter((x) => x.parentId === n.id)
  const people = affiliations.filter((a) => a.orgId === n.id)
  const merges = candidates.filter(
    (c) => c.status === "candidate" && (c.identityAId === n.id || c.identityBId === n.id),
  )

  async function run(label: string, fn: () => Promise<unknown>): Promise<void> {
    setBusy(true)
    try {
      await fn()
      toast.success(label)
      onChanged()
    } catch (e) {
      toast.error(`${label}: ${errMsg(e)}`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Panel>
      <PanelHeader
        eyebrow={n.kind === "producto" ? "producto" : "organización"}
        title={
          <span className="flex items-center gap-2">
            {n.displayName}
            {n.pinned && <Flag kind="ok">confirmada</Flag>}
            {n.disputed && <Flag kind="error">en disputa</Flag>}
            {n.weak && <Flag kind="warn">débil</Flag>}
          </span>
        }
        sub={
          n.parentId != null ? (
            <span>
              pertenece a{" "}
              <button
                type="button"
                className="text-brand hover:underline"
                onClick={() => onSelect(n.parentId as number)}
              >
                {n.parentName}
              </button>{" "}
              · {n.messages === 0 ? "sin correo" : `${n.messages} ${n.messages === 1 ? "correo" : "correos"}`} ·
              confianza {n.bestConfidence.toFixed(2)} · {n.methods.join("/") || n.parentSource}
            </span>
          ) : (
            <span>raíz (sin padre) · {n.mentionCount} menciones</span>
          )
        }
        right={
          <div className="flex flex-wrap gap-1.5">
            {n.parentId != null && !n.pinned && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => run("Padre confirmado", () => confirmParent(n.id))}
              >
                <Pin className="size-3.5" /> Confirmar padre
              </Button>
            )}
            {n.parentId != null && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() =>
                  run("Padre quitado", () => updateIdentity(n.id, { parentId: null }))
                }
              >
                <X className="size-3.5" /> Quitar padre
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() =>
                run("Derivado desde el libro", async () => {
                  const changed = await deriveHierarchy(n.id)
                  if (changed.length === 0) toast.message("Sin cambios: el padre ya es el más sustentado")
                })
              }
            >
              <RefreshCw className="size-3.5" /> Derivar
            </Button>
          </div>
        }
      />
      <PanelBody className="space-y-5">
        {/* candidatos */}
        <section>
          <div className="eyebrow mb-1.5">Padres candidatos (del libro)</div>
          {n.candidates.length === 0 && n.unresolvedNames.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Ningún correo propone un padre. {n.parentId != null ? "El padre actual viene de antes del libro (legacy) o de una decisión manual." : ""}
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-[11px] uppercase tracking-wide text-muted-foreground">
                <tr>
                  <th className="py-1 font-medium">padre</th>
                  <th className="py-1 font-medium">correos</th>
                  <th className="py-1 font-medium">soporte</th>
                  <th className="py-1 font-medium">mejor conf.</th>
                  <th className="py-1 font-medium">vía</th>
                  <th className="py-1" />
                </tr>
              </thead>
              <tbody>
                {n.candidates.map((c) => (
                  <tr key={c.targetId} className="border-t border-border/60">
                    <td className="py-1.5">
                      <button
                        type="button"
                        className="text-left hover:underline"
                        onClick={() => onSelect(c.targetId)}
                      >
                        {c.targetName}
                      </button>
                      {c.targetId === n.parentId && (
                        <span className="ml-1.5 text-[11px] text-status-ok">actual</span>
                      )}
                    </td>
                    <td className="num py-1.5">{c.messages}</td>
                    <td className="num py-1.5">{c.support.toFixed(2)}</td>
                    <td className="num py-1.5">{c.bestConfidence.toFixed(2)}</td>
                    <td className="py-1.5 text-[11px] text-muted-foreground">
                      {c.methods.join("/")}
                      {c.pinned ? " · pin" : ""}
                    </td>
                    <td className="py-1.5 text-right">
                      {c.targetId !== n.parentId && (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() =>
                            run(`Colgada de ${c.targetName}`, () =>
                              updateIdentity(n.id, { parentId: c.targetId }),
                            )
                          }
                        >
                          <Check className="size-3.5" /> Elegir
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
                {n.unresolvedNames.map((name) => (
                  <tr key={`u:${name}`} className="border-t border-border/60">
                    <td className="py-1.5" colSpan={5}>
                      <span className="text-muted-foreground">nombrado, sin ficha:</span>{" "}
                      <span className="font-medium">{name}</span>
                      <span className="ml-2 text-[11px] text-muted-foreground">
                        el curador o vos deciden si existe y cuál es
                      </span>
                    </td>
                    <td />
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        {/* fusiones */}
        {merges.length > 0 && (
          <section>
            <div className="eyebrow mb-1.5">Posibles duplicados</div>
            <ul className="space-y-1 text-sm">
              {merges.map((m) => {
                const otherId = m.identityAId === n.id ? m.identityBId : m.identityAId
                const otherName = m.identityAId === n.id ? m.bName : m.aName
                return (
                  <li key={m.id} className="flex items-center gap-2">
                    <GitMerge className="size-3.5 text-muted-foreground" />
                    <span>
                      ¿misma entidad que <span className="font-medium">{otherName}</span>?
                    </span>
                    <span className="num text-[11px] text-muted-foreground">
                      {m.reason} {m.score != null ? m.score.toFixed(2) : ""}
                    </span>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() =>
                        run(`Fusionada en ${n.displayName}`, () => mergeIdentities(n.id, otherId))
                      }
                    >
                      Fusionar acá
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy}
                      onClick={() =>
                        run(`Fusionada en ${otherName}`, () => mergeIdentities(otherId, n.id))
                      }
                    >
                      Fusionar allá
                    </Button>
                  </li>
                )
              })}
            </ul>
          </section>
        )}

        {/* reclamos */}
        <section>
          <div className="mb-1.5 flex items-center justify-between">
            <div className="eyebrow">Libro de evidencia</div>
            <label className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
              <input
                type="checkbox"
                checked={includeRejected}
                onChange={(e) => setIncludeRejected(e.target.checked)}
              />
              ver rechazados
            </label>
          </div>
          {claims.error ? (
            <ErrorState detail={claims.error} />
          ) : claims.loading && !claims.data ? (
            <p className="text-sm text-muted-foreground">Cargando reclamos…</p>
          ) : claims.data && claims.data.outgoing.length + claims.data.incoming.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Sin reclamos todavía: ningún correo procesado con el libro activo mencionó un vínculo
              de esta entrada.
            </p>
          ) : (
            claims.data && (
              <ul>
                {[...claims.data.outgoing, ...claims.data.incoming].map((c) => (
                  <ClaimRow
                    key={c.id}
                    c={c}
                    self={n.id}
                    busy={busy}
                    onToggle={(cl) =>
                      run(
                        cl.status === "rejected" ? "Reclamo reactivado" : "Reclamo rechazado",
                        () => rejectClaim(cl.id, cl.status === "rejected"),
                      )
                    }
                  />
                ))}
              </ul>
            )
          )}
        </section>

        {/* personas */}
        {people.length > 0 && (
          <section>
            <div className="eyebrow mb-1.5">Personas afiliadas</div>
            <ul className="space-y-1 text-sm">
              {people.map((a) => (
                <li key={a.personId} className="flex flex-wrap items-center gap-x-2">
                  <Users className="size-3.5 text-muted-foreground" />
                  <span className="font-medium">{a.personName}</span>
                  {a.role && <span className="text-muted-foreground">· {a.role}</span>}
                  <span className="num text-[11px] text-muted-foreground">
                    {a.messages} {a.messages === 1 ? "correo" : "correos"}
                  </span>
                  {a.roles.length > 1 && (
                    <span className="text-[11px] text-muted-foreground">
                      variantes: {a.roles.filter((r) => r !== a.role).join(" · ")}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

        {/* hijos */}
        {children.length > 0 && (
          <section>
            <div className="eyebrow mb-1.5">Sub-unidades ({children.length})</div>
            <ul className="space-y-0.5 text-sm">
              {children
                .slice()
                .sort((a, b) => a.displayName.localeCompare(b.displayName))
                .map((k) => (
                  <li key={k.id} className="flex items-center gap-2">
                    <button
                      type="button"
                      className="hover:underline"
                      onClick={() => onSelect(k.id)}
                    >
                      {k.displayName}
                    </button>
                    <LinkMarks n={k} />
                  </li>
                ))}
            </ul>
          </section>
        )}
      </PanelBody>
    </Panel>
  )
}

// ---- Curador ----------------------------------------------------------------------------------

function CuratorBar({ onDone }: { onDone: () => void }) {
  const [scope, setScope] = useState<CurateInput["scope"]>("all")
  const [dryRun, setDryRun] = useState(true)
  const [web, setWeb] = useState(true)
  const [busy, setBusy] = useState<"curate" | "derive" | null>(null)
  const [result, setResult] = useState<CurateResult | null>(null)

  async function curate(): Promise<void> {
    setBusy("curate")
    try {
      const r = await curateHierarchy({
        scope,
        dryRun,
        webSearch: web,
        limit: 40,
        rounds: scope === "tree" && !dryRun ? 3 : 1,
      })
      setResult(r)
      toast.success(
        `${dryRun ? "Dry-run" : "Curado"}: ${r.items} ítems, ${r.decisions} decisiones` +
          (dryRun ? "" : ` · colgó ${r.set}, mantuvo ${r.kept}, fundió ${r.merged}`),
      )
      if (!dryRun) onDone()
    } catch (e) {
      toast.error(`Curador: ${errMsg(e)}`)
    } finally {
      setBusy(null)
    }
  }

  async function derive(): Promise<void> {
    setBusy("derive")
    try {
      const changed = await deriveHierarchy()
      toast.success(`Derivado: ${changed.length} padre(s) cambiaron`)
      onDone()
    } catch (e) {
      toast.error(`Derivar: ${errMsg(e)}`)
    } finally {
      setBusy(null)
    }
  }

  return (
    <Panel>
      <PanelBody className="space-y-3">
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className="eyebrow">curador codex</span>
          <select
            className="rounded border border-input bg-background px-2 py-1 text-sm"
            value={scope}
            onChange={(e) => setScope(e.target.value as CurateInput["scope"])}
          >
            {SCOPES.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
          <label className="flex items-center gap-1.5 text-[12px]">
            <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
            solo proponer (dry-run)
          </label>
          <label className="flex items-center gap-1.5 text-[12px]">
            <input type="checkbox" checked={web} onChange={(e) => setWeb(e.target.checked)} />
            contrastar en la web (solo orgs)
          </label>
          <Button size="sm" disabled={busy !== null} onClick={() => void curate()}>
            <Sparkles className="size-3.5" /> {busy === "curate" ? "Curando… (≈1 min por tanda)" : "Curar"}
          </Button>
          <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => void derive()}>
            <RefreshCw className="size-3.5" /> {busy === "derive" ? "Derivando…" : "Derivar padres desde el libro"}
          </Button>
        </div>
        {result && (
          <div className="rounded border border-border bg-muted/30 p-3 text-sm">
            <div className="mb-1.5 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-muted-foreground">
              <span>{result.dryRun ? "propuesta (nada aplicado)" : "aplicado"}</span>
              <span className="num">{result.items} ítems</span>
              <span className="num">{result.batches} tandas</span>
              <span className="num">{Math.round(result.latencyMs / 1000)} s</span>
              {!result.dryRun && (
                <>
                  <span className="num">mantuvo {result.kept}</span>
                  <span className="num">colgó {result.set}</span>
                  <span className="num">creó {result.created}</span>
                  <span className="num">raíz {result.cleared}</span>
                  <span className="num">fundió {result.merged}</span>
                  <span className="num">borró {result.deleted}</span>
                  <span className="num">saltó {result.skipped}</span>
                  <span className="num">descartadas {result.dropped}</span>
                </>
              )}
              {result.errors > 0 && (
                <span className="text-status-error">{result.errors} tanda(s) fallaron</span>
              )}
            </div>
            {result.applied.length === 0 ? (
              <p className="text-muted-foreground">Sin decisiones.</p>
            ) : (
              <ul className="max-h-64 space-y-0.5 overflow-y-auto">
                {result.applied.map((a, i) => (
                  <li key={`${a.item}-${i}`} className="flex flex-wrap gap-x-2">
                    <span className="font-mono text-[11px] text-muted-foreground">{a.item}</span>
                    <span className="font-medium">{a.action}</span>
                    <span className="text-muted-foreground">→ {a.result}</span>
                    {a.parentName && <span>«{a.parentName}»</span>}
                    {a.confidence != null && (
                      <span className="num text-[11px] text-muted-foreground">
                        {a.confidence.toFixed(2)}
                      </span>
                    )}
                    {a.rationale && <span className="text-muted-foreground">{a.rationale}</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </PanelBody>
    </Panel>
  )
}

// ---- Página -----------------------------------------------------------------------------------

export function JerarquiaPage() {
  const [version, setVersion] = useState(0)
  const bump = (): void => setVersion((v) => v + 1)
  const [filter, setFilter] = useState<Filter>("all")
  const [q, setQ] = useState("")
  const [selected, setSelected] = useState<number | null>(null)
  const [expanded, setExpanded] = useState<Set<number>>(new Set())

  const data = useAsync(fetchHierarchy, [version])
  const cands = useAsync(fetchMergeCandidates, [version])
  const nodes = useMemo(() => data.data?.nodes ?? [], [data.data])
  const affiliations = useMemo(() => data.data?.affiliations ?? [], [data.data])

  const childrenOf = useMemo(() => {
    const m = new Map<number, HierarchyNode[]>()
    for (const n of nodes) {
      if (n.parentId != null) {
        const arr = m.get(n.parentId) ?? []
        arr.push(n)
        m.set(n.parentId, arr)
      }
    }
    for (const arr of m.values()) arr.sort((a, b) => a.displayName.localeCompare(b.displayName))
    return m
  }, [nodes])
  const affCount = useMemo(() => {
    const m = new Map<number, number>()
    for (const a of affiliations) m.set(a.orgId, (m.get(a.orgId) ?? 0) + 1)
    return m
  }, [affiliations])
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes])

  const term = q.trim().toLowerCase()
  const listed = nodes.filter(
    (n) => matches(n, filter) && (!term || n.displayName.toLowerCase().includes(term)),
  )
  const roots = listed
    .filter((n) => filter !== "all" || term || n.parentId == null)
    .sort((a, b) => {
      const ak = childrenOf.get(a.id)?.length ?? 0
      const bk = childrenOf.get(b.id)?.length ?? 0
      if ((ak > 0) !== (bk > 0)) return ak > 0 ? -1 : 1
      return a.displayName.localeCompare(b.displayName)
    })
  const treeMode = filter === "all" && !term

  const counts = {
    total: nodes.length,
    linked: nodes.filter((n) => n.parentId != null).length,
    disputed: nodes.filter((n) => n.disputed).length,
    weak: nodes.filter((n) => n.weak).length,
    unresolved: nodes.filter((n) => n.unresolvedNames.length > 0).length,
    roots: nodes.filter(isSuspiciousRoot).length,
    pinned: nodes.filter((n) => n.pinned).length,
  }

  function toggle(id: number): void {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  function select(id: number): void {
    setSelected(id)
    // Abrir el camino hasta el nodo para que se vea en el árbol.
    setExpanded((prev) => {
      const next = new Set(prev)
      let cur = byId.get(id)?.parentId ?? null
      while (cur != null && !next.has(cur)) {
        next.add(cur)
        cur = byId.get(cur)?.parentId ?? null
      }
      return next
    })
  }

  const current = selected != null ? (byId.get(selected) ?? null) : null

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="módulo · identidades"
        title="Jerarquía"
        description="Quién pertenece a quién y quién es miembro de qué, con la evidencia detrás de cada vínculo: cuántos correos lo sustentan, con qué confianza y por qué vía. El padre efectivo se deriva del libro (gana el más sustentado; lo que confirmás queda fijo). Lo dudoso se marca y el curador codex lo resuelve con la evidencia y, para organizaciones, contrastando en la web."
      />
      <CuratorBar onDone={bump} />
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {FILTERS.map((f) => {
          const n =
            f.key === "all"
              ? counts.total
              : f.key === "disputed"
                ? counts.disputed
                : f.key === "weak"
                  ? counts.weak
                  : f.key === "unresolved"
                    ? counts.unresolved
                    : f.key === "roots"
                      ? counts.roots
                      : counts.pinned
          return (
            <button
              key={f.key}
              type="button"
              onClick={() => setFilter(f.key)}
              aria-pressed={filter === f.key}
              className={cn(
                "rounded-full border px-3 py-1 text-[12px]",
                filter === f.key
                  ? "border-brand bg-brand text-background"
                  : "border-input hover:bg-muted/40",
              )}
            >
              {f.label} <span className="num opacity-70">{n}</span>
            </button>
          )
        })}
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Buscar organización…"
          className="ml-auto w-56 rounded border border-input bg-background px-2 py-1 text-sm"
        />
        <span className="eyebrow">
          {counts.linked}/{counts.total} con padre
        </span>
      </div>
      <div className="grid gap-5 xl:grid-cols-5">
        <Panel className="overflow-hidden xl:col-span-2">
          <PanelHeader
            eyebrow="pertenencia"
            title={treeMode ? "Árbol" : `${listed.length} entradas`}
            sub={
              treeMode
                ? "Raíces con sub-unidades primero. Cada vínculo: correos que lo sustentan · vía."
                : "Lista plana con el padre actual de cada una."
            }
          />
          <PanelBody className="p-0">
            {data.error ? (
              <ErrorState detail={data.error} />
            ) : data.loading && !data.data ? (
              <p className="p-4 text-sm text-muted-foreground">Cargando jerarquía…</p>
            ) : roots.length === 0 ? (
              <EmptyState
                icon={<Network className="size-5" />}
                title="Nada que mostrar"
                hint={
                  filter === "all"
                    ? "Cuando haya organizaciones o productos, acá va el árbol."
                    : "Ninguna entrada con esa marca. Buena señal."
                }
              />
            ) : (
              <ul className="max-h-[70vh] overflow-y-auto py-1">
                {roots.map((r) =>
                  treeMode ? (
                    <TreeRow
                      key={r.id}
                      n={r}
                      depth={0}
                      childrenOf={childrenOf}
                      affCount={affCount}
                      selected={selected}
                      onSelect={select}
                      expanded={expanded}
                      onToggle={toggle}
                    />
                  ) : (
                    <li key={r.id}>
                      <button
                        type="button"
                        onClick={() => select(r.id)}
                        className={cn(
                          "flex w-full flex-wrap items-center gap-x-2 gap-y-0.5 px-3 py-1.5 text-left text-sm hover:bg-muted/40",
                          selected === r.id && "bg-brand/10",
                        )}
                      >
                        <Building2 className="size-3.5 shrink-0 text-muted-foreground" />
                        <span className="font-medium">{r.displayName}</span>
                        {r.parentId != null ? (
                          <span className="text-muted-foreground">→ {r.parentName}</span>
                        ) : (
                          <span className="text-muted-foreground">(raíz)</span>
                        )}
                        {r.unresolvedNames.length > 0 && (
                          <span className="text-[11px] text-status-review">
                            nombra: {r.unresolvedNames.join(", ")}
                          </span>
                        )}
                        <span className="ml-auto">
                          <LinkMarks n={r} />
                        </span>
                      </button>
                    </li>
                  ),
                )}
              </ul>
            )}
          </PanelBody>
        </Panel>
        <div className="xl:col-span-3">
          {current ? (
            <NodeDetail
              n={current}
              nodes={nodes}
              affiliations={affiliations}
              candidates={cands.data ?? []}
              version={version}
              onChanged={bump}
              onSelect={select}
            />
          ) : (
            <Panel>
              <PanelBody>
                <EmptyState
                  icon={<Network className="size-5" />}
                  title="Elegí una entrada"
                  hint="Vas a ver sus padres candidatos con soporte, los correos que sustentan cada vínculo, sus personas y sus sub-unidades, con acciones para confirmar, corregir o rechazar."
                />
              </PanelBody>
            </Panel>
          )}
        </div>
      </div>
    </div>
  )
}
