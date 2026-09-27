// /prompts — ver y editar los prompts del sistema, con confirmación antes de aplicar.
//
// El código es el default; lo que se guarda acá es un override que rige en todos los procesos en
// menos de un minuto. Cada guardado deja una versión (se puede volver). «Restablecer al código»
// quita el override. Nada se aplica sin pasar por el diálogo de confirmación.

import { useState } from "react"
import { toast } from "sonner"
import { PageHeader } from "@/components/common/page-header"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import {
  fetchPrompt,
  fetchPrompts,
  resetPrompt,
  revertPrompt,
  savePrompt,
  type PromptDetail,
  type PromptSummary,
  type PromptVersion,
} from "@/data"
import { ApiError } from "@/lib/api"
import { describeDiff, diffSummary } from "@/lib/prompts-diff"
import { useAsync } from "@/lib/use-async"
import { cn } from "@/lib/utils"

type DialogKind = { kind: "save" } | { kind: "reset" } | { kind: "revert"; version: PromptVersion } | null

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return e.message
  return e instanceof Error ? e.message : String(e)
}

function fmtDate(iso: string | null): string {
  if (!iso) return ""
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("es-CO", { dateStyle: "short", timeStyle: "short" })
}

export function PromptsPage() {
  const list = useAsync(() => fetchPrompts(), [])
  const [chosenKey, setChosenKey] = useState<string | null>(null)
  // Ediciones sin guardar, por clave: así cambiar de prompt y volver no pierde lo escrito, y no
  // hace falta sincronizar estado con efectos.
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [note, setNote] = useState("")
  const [dialog, setDialog] = useState<DialogKind>(null)
  const [busy, setBusy] = useState(false)

  const items = list.data?.items ?? []
  const groups = list.data?.groups ?? []
  const selectedKey = chosenKey ?? items[0]?.key ?? null
  const det = useAsync<PromptDetail | null>(
    () => (selectedKey ? fetchPrompt(selectedKey) : Promise.resolve(null)),
    [selectedKey],
  )
  const detail = det.data
  const detailError = det.error ? errMsg(det.error) : null
  const draft = (selectedKey && edits[selectedKey]) ?? detail?.effective ?? ""
  const setDraft = (value: string) => {
    if (selectedKey) setEdits((prev) => ({ ...prev, [selectedKey]: value }))
  }

  const applyDetail = (d: PromptDetail) => {
    setEdits((prev) => {
      const next = { ...prev }
      delete next[d.key]
      return next
    })
    setNote("")
    setDialog(null)
    det.reload()
    list.reload()
  }

  const diff = detail ? diffSummary(detail.effective, draft) : null
  const dirty = !!diff && !diff.same
  const looksLikeCode = !!detail && diffSummary(detail.default, draft).same

  async function run(action: () => Promise<PromptDetail>, ok: string) {
    setBusy(true)
    try {
      applyDetail(await action())
      toast.success(ok)
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow="proceso · prompts"
        title="Prompts"
        description="Lo que se le pide a cada modelo: extractores, jueces, enrutador. El código es el valor por defecto; lo que guardes acá lo reemplaza en todos los procesos en menos de un minuto y queda versionado. Nada se aplica sin confirmar."
      />
      <div className="grid gap-5 lg:grid-cols-[minmax(240px,1fr)_3fr]">
        <Panel>
          <PanelHeader title="Prompts" sub={`${items.length} en el registro`} />
          <PanelBody className="max-h-[70vh] overflow-y-auto p-0">
            {list.error && <p className="p-3 text-sm text-status-error">{errMsg(list.error)}</p>}
            {groups.map((g) => (
              <div key={g}>
                <div className="eyebrow border-b border-border bg-muted/40 px-3 py-1.5">{g}</div>
                {items
                  .filter((p) => p.group === g)
                  .map((p) => (
                    <PromptRow
                      key={p.key}
                      p={p}
                      active={p.key === selectedKey}
                      onClick={() => setChosenKey(p.key)}
                    />
                  ))}
              </div>
            ))}
          </PanelBody>
        </Panel>

        <Panel>
          {detail ? (
            <>
              <PanelHeader
                eyebrow={
                  <span className="flex flex-wrap items-center gap-1.5">
                    <span>{detail.group}</span>
                    {detail.consumer && <Badge variant="outline">consumidor: {detail.consumer}</Badge>}
                    {detail.hasOverride ? (
                      <Badge title={`Editado ${fmtDate(detail.updatedAt)}${detail.note ? ` · ${detail.note}` : ""}`}>
                        modificado
                      </Badge>
                    ) : (
                      <Badge variant="outline">como en el código</Badge>
                    )}
                    {!detail.editable && <Badge variant="outline">solo referencia</Badge>}
                  </span>
                }
                title={detail.title}
                sub={detail.description}
                right={
                  <span className="num text-xs text-muted-foreground">{draft.length} caracteres</span>
                }
              />
              <PanelBody className="space-y-3">
                {detail.warnings.length > 0 && (
                  <ul className="space-y-1 rounded border border-status-warning/50 bg-status-warning/10 p-2 text-xs">
                    {detail.warnings.map((w) => (
                      <li key={w}>⚠ {w}</li>
                    ))}
                  </ul>
                )}
                <Textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  readOnly={!detail.editable}
                  spellCheck={false}
                  className="min-h-[48vh] w-full resize-y font-mono text-[12px] leading-relaxed"
                />
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    disabled={!detail.editable || !dirty || busy}
                    onClick={() => setDialog({ kind: "save" })}
                  >
                    Guardar cambios…
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={!dirty || busy}
                    onClick={() =>
                      setEdits((prev) => {
                        const next = { ...prev }
                        delete next[detail.key]
                        return next
                      })
                    }
                  >
                    Descartar edición
                  </Button>
                  {detail.hasOverride && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() => setDialog({ kind: "reset" })}
                    >
                      Restablecer al código…
                    </Button>
                  )}
                  {dirty && diff && (
                    <span className="text-xs text-muted-foreground">{describeDiff(diff)}</span>
                  )}
                  {dirty && looksLikeCode && (
                    <span className="text-xs text-muted-foreground">
                      (igual al código: guardar equivale a restablecer)
                    </span>
                  )}
                </div>
                {detail.versions.length > 0 && (
                  <div>
                    <div className="eyebrow mb-1">Versiones</div>
                    <ul className="divide-y divide-border rounded border border-border text-xs">
                      {detail.versions.map((v) => (
                        <li key={v.id} className="flex items-center justify-between gap-2 px-2 py-1.5">
                          <span className="min-w-0 truncate">
                            <span className="num text-muted-foreground">{fmtDate(v.createdAt)}</span>
                            {" · "}
                            {v.note || "sin nota"}
                            <span className="num text-muted-foreground"> · {v.chars} car.</span>
                          </span>
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={busy || !detail.editable}
                            onClick={() => setDialog({ kind: "revert", version: v })}
                          >
                            Volver a esta versión…
                          </Button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </PanelBody>
            </>
          ) : (
            <PanelBody>
              <p className="text-sm text-muted-foreground">
                {detailError ?? (selectedKey ? "Cargando…" : "Elegí un prompt.")}
              </p>
            </PanelBody>
          )}
        </Panel>
      </div>

      {/* --- confirmaciones ------------------------------------------------------------- */}
      <Dialog open={dialog?.kind === "save"} onOpenChange={(o) => !o && setDialog(null)}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Aplicar el cambio a «{detail?.title}»</DialogTitle>
            <DialogDescription>
              {diff ? describeDiff(diff) : ""} Rige para todos los procesos en menos de un minuto y
              queda una versión para volver atrás.
            </DialogDescription>
          </DialogHeader>
          {diff && (diff.added.length > 0 || diff.removed.length > 0) && (
            <div className="max-h-56 space-y-2 overflow-y-auto rounded border border-border p-2 font-mono text-[11px]">
              {diff.removed.slice(0, 8).map((l, i) => (
                <div key={`r${i}`} className="text-status-error">− {l}</div>
              ))}
              {diff.added.slice(0, 8).map((l, i) => (
                <div key={`a${i}`} className="text-status-ok">+ {l}</div>
              ))}
              {diff.added.length + diff.removed.length > 16 && (
                <div className="text-muted-foreground">… y más líneas</div>
              )}
            </div>
          )}
          <Input
            placeholder="Nota (qué cambiaste y por qué; queda en el historial)"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialog(null)} disabled={busy}>
              Cancelar
            </Button>
            <Button
              disabled={busy || !detail}
              onClick={() => detail && run(() => savePrompt(detail.key, draft, note), "Prompt aplicado")}
            >
              Confirmar y aplicar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={dialog?.kind === "reset"} onOpenChange={(o) => !o && setDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Restablecer «{detail?.title}» al código</DialogTitle>
            <DialogDescription>
              Se quita el override y vuelve a regir el texto del código. El historial de versiones
              se conserva.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialog(null)} disabled={busy}>
              Cancelar
            </Button>
            <Button
              disabled={busy || !detail}
              onClick={() => detail && run(() => resetPrompt(detail.key), "Vuelve a regir el código")}
            >
              Confirmar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={dialog?.kind === "revert"} onOpenChange={(o) => !o && setDialog(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Volver a una versión anterior</DialogTitle>
            <DialogDescription>
              {dialog?.kind === "revert"
                ? `Versión del ${fmtDate(dialog.version.createdAt)}${dialog.version.note ? ` («${dialog.version.note}»)` : ""}, ${dialog.version.chars} caracteres. Pasa a regir ya y queda como versión nueva.`
                : ""}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialog(null)} disabled={busy}>
              Cancelar
            </Button>
            <Button
              disabled={busy || !detail || dialog?.kind !== "revert"}
              onClick={() =>
                detail &&
                dialog?.kind === "revert" &&
                run(() => revertPrompt(detail.key, dialog.version.id), "Versión aplicada")
              }
            >
              Confirmar
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function PromptRow({ p, active, onClick }: { p: PromptSummary; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex w-full items-center justify-between gap-2 border-b border-border px-3 py-2 text-left text-sm transition hover:bg-muted/50",
        active && "bg-brand/10",
      )}
    >
      <span className="min-w-0 truncate">{p.title}</span>
      <span className="flex shrink-0 items-center gap-1">
        {!p.editable && <Badge variant="outline">ref.</Badge>}
        {p.hasOverride && <Badge title={p.note || undefined}>modificado</Badge>}
      </span>
    </button>
  )
}
