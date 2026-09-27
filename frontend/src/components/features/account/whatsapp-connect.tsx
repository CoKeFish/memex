// «Vincular WhatsApp» + selector de chats (un Dialog, multi-paso).
// Vincular: número → código de 8 caracteres que se escribe en el teléfono (WhatsApp → Dispositivos
// vinculados → Vincular un dispositivo → Vincular con número de teléfono). Mientras, la UI consulta
// el estado. Luego: el catálogo de chats que la sesión fue viendo, con interruptor por chat,
// naturaleza (anuncios | programa | íntimo) y «desde». Solo lo incluido entra al inbox.

import { Loader2, Plug, RefreshCw, Unplug } from "lucide-react"
import { type FormEvent, useEffect, useRef, useState } from "react"
import { toast } from "sonner"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  type ChatMode,
  getWhatsAppStatus,
  listWhatsAppChats,
  type ManagedAccount,
  pairWhatsApp,
  saveWhatsAppChats,
  unlinkWhatsApp,
  type WhatsAppChat,
  type WhatsAppStatus,
} from "@/data/accounts"
import { ApiError } from "@/lib/api"

const inputCls =
  "rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:border-brand"
const btnCls =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs hover:bg-accent/40 disabled:opacity-50"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : "Algo salió mal"
}

type Step = "phone" | "code" | "chats"

const STATUS_LABEL: Record<WhatsAppStatus["status"], string> = {
  sin_vincular: "sin vincular",
  codigo: "esperando el código en el teléfono",
  vinculado: "vinculado",
  desconectado: "desconectado (reconecta solo)",
  cerrado: "sesión cerrada",
  baneado: "WhatsApp suspendió este dispositivo",
}

function fmtDay(iso: string | null): string {
  if (!iso) return ""
  return iso.slice(0, 10)
}

export function WhatsAppConnect({
  account,
  onDone,
}: {
  account: ManagedAccount
  onDone: () => void | Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [step, setStep] = useState<Step>("phone")
  const [busy, setBusy] = useState(false)
  const [phone, setPhone] = useState("")
  const [code, setCode] = useState("")
  const [status, setStatus] = useState<WhatsAppStatus | null>(null)
  const [chats, setChats] = useState<WhatsAppChat[]>([])
  // chatId → selección (presencia = incluido).
  const [picks, setPicks] = useState<Map<string, { mode: ChatMode; since: string }>>(new Map())
  const [confirmUnlink, setConfirmUnlink] = useState(false)
  const pollRef = useRef<number | null>(null)

  function stopPolling(): void {
    if (pollRef.current != null) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
  }

  useEffect(() => stopPolling, [])

  async function loadChats(): Promise<void> {
    const rows = await listWhatsAppChats(account.id)
    setChats(rows)
    setPicks(
      new Map(
        rows
          .filter((c) => c.included)
          .map((c) => [c.chatId, { mode: c.mode ?? "anuncios", since: c.since ?? "" }]),
      ),
    )
  }

  async function refreshStatus(): Promise<WhatsAppStatus> {
    const s = await getWhatsAppStatus(account.id)
    setStatus(s)
    return s
  }

  function startPolling(): void {
    stopPolling()
    pollRef.current = window.setInterval(async () => {
      try {
        const s = await refreshStatus()
        if (s.status === "vinculado") {
          stopPolling()
          await loadChats()
          setStep("chats")
          toast.success("WhatsApp vinculado")
        } else if (s.status === "baneado" || s.status === "cerrado") {
          stopPolling()
        }
      } catch {
        // siguiente tick
      }
    }, 3000)
  }

  async function start(): Promise<void> {
    setOpen(true)
    setBusy(true)
    setConfirmUnlink(false)
    try {
      const s = await refreshStatus()
      if (s.status === "vinculado" || s.status === "desconectado") {
        await loadChats()
        setStep("chats")
      } else if (s.status === "codigo" && s.pairCode) {
        setCode(s.pairCode)
        setStep("code")
        startPolling()
      } else {
        setStep("phone")
      }
    } catch (e) {
      toast.error(errMsg(e))
      setOpen(false)
    } finally {
      setBusy(false)
    }
  }

  async function onSubmitPhone(ev: FormEvent): Promise<void> {
    ev.preventDefault()
    setBusy(true)
    try {
      const c = await pairWhatsApp(account.id, phone.trim())
      setCode(c)
      setStep("code")
      startPolling()
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  function toggle(chat: WhatsAppChat): void {
    setPicks((prev) => {
      const next = new Map(prev)
      if (next.has(chat.chatId)) next.delete(chat.chatId)
      else next.set(chat.chatId, { mode: "anuncios", since: "" })
      return next
    })
  }

  function setPick(chatId: string, patch: Partial<{ mode: ChatMode; since: string }>): void {
    setPicks((prev) => {
      const next = new Map(prev)
      const cur = next.get(chatId) ?? { mode: "anuncios" as ChatMode, since: "" }
      next.set(chatId, { ...cur, ...patch })
      return next
    })
  }

  async function save(): Promise<void> {
    setBusy(true)
    try {
      const byId = new Map(chats.map((c) => [c.chatId, c]))
      const r = await saveWhatsAppChats(
        account.id,
        [...picks].map(([chatId, p]) => ({
          chatId,
          name: byId.get(chatId)?.name ?? "",
          mode: p.mode,
          since: p.since || null,
        })),
      )
      toast.success(
        r.ingested
          ? `${r.saved} chat(s) guardados · ${r.ingested} mensajes de la bandeja entraron`
          : `${r.saved} chat(s) guardados`,
      )
      await loadChats()
      await onDone()
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  async function unlink(): Promise<void> {
    setBusy(true)
    try {
      await unlinkWhatsApp(account.id)
      toast.success("Sesión de WhatsApp cerrada")
      setConfirmUnlink(false)
      setStep("phone")
      await refreshStatus()
      await onDone()
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button type="button" className={btnCls} onClick={start}>
        <Plug className="size-3" /> Vincular / Chats
      </button>
      <Dialog
        open={open}
        onOpenChange={(o) => {
          setOpen(o)
          if (!o) stopPolling()
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>WhatsApp · {account.alias}</DialogTitle>
            <DialogDescription>
              {step === "phone" &&
                "memex queda como un dispositivo vinculado, igual que WhatsApp Web. Nunca envía nada."}
              {step === "code" && "Escribí este código en el teléfono. Dura pocos minutos."}
              {step === "chats" && "Elegí qué chats entran. Lo demás queda en espera 30 días."}
            </DialogDescription>
          </DialogHeader>

          {status && (
            <p className="text-xs text-muted-foreground">
              Estado: {STATUS_LABEL[status.status]}
              {status.status === "vinculado" && (status.connected ? " · conectado" : " · sin conexión")}
              {status.lastError ? ` · ${status.lastError}` : ""}
            </p>
          )}

          {busy && step !== "chats" && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-3 animate-spin" /> trabajando…
            </div>
          )}

          {step === "phone" && (
            <form onSubmit={onSubmitPhone} className="flex items-end gap-2">
              <label className="space-y-1">
                <span className="eyebrow">número con indicativo, sin +</span>
                <input
                  className={`${inputCls} w-48`}
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  placeholder="57300XXXXXXX"
                  inputMode="numeric"
                  autoFocus
                />
              </label>
              <button type="submit" disabled={busy || phone.trim().length < 8} className={btnCls}>
                Pedir código
              </button>
            </form>
          )}

          {step === "code" && (
            <div className="space-y-2">
              <p className="font-mono text-2xl tracking-widest" aria-label="código de vinculación">
                {code}
              </p>
              <ol className="list-decimal space-y-0.5 pl-5 text-xs text-muted-foreground">
                <li>WhatsApp en el teléfono → Dispositivos vinculados</li>
                <li>Vincular un dispositivo → Vincular con número de teléfono</li>
                <li>Escribí el código de arriba</li>
              </ol>
              <p className="text-xs text-muted-foreground">
                Esperando al teléfono… si el código venció, pedí otro.
              </p>
              <button type="button" className={btnCls} onClick={() => setStep("phone")}>
                Pedir otro código
              </button>
            </div>
          )}

          {step === "chats" && (
            <div className="space-y-2">
              {chats.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  Todavía no llegó el catálogo. Recién vinculado, WhatsApp tarda unos segundos en
                  mandar el historial; refrescá.
                </p>
              ) : (
                <div className="max-h-80 space-y-1 overflow-y-auto rounded-md border border-border p-2">
                  {chats.map((c) => {
                    const pick = picks.get(c.chatId)
                    return (
                      <div key={c.chatId} className="flex flex-wrap items-center gap-2 text-sm">
                        <input
                          type="checkbox"
                          checked={pick != null}
                          onChange={() => toggle(c)}
                          aria-label={`incluir ${c.name || c.chatId}`}
                        />
                        <span className="truncate" title={c.chatId}>
                          {c.name || c.chatId}
                        </span>
                        <span className="eyebrow text-muted-foreground">
                          {c.kind === "group" ? "grupo" : c.kind === "user" ? "1:1" : c.kind}
                          {" · "}
                          {c.msgCount} msg
                          {c.lastAt ? ` · hasta ${fmtDay(c.lastAt)}` : ""}
                          {c.buffered ? ` · ${c.buffered} en espera` : ""}
                        </span>
                        {pick && (
                          <>
                            <select
                              className="h-6 rounded border border-border bg-background px-1 text-[11px]"
                              aria-label="Naturaleza del chat"
                              value={pick.mode}
                              onChange={(e) => setPick(c.chatId, { mode: e.target.value as ChatMode })}
                            >
                              <option value="anuncios">anuncios</option>
                              <option value="programa">programa</option>
                              <option value="intimo">íntimo</option>
                            </select>
                            <input
                              type="date"
                              className="h-6 rounded border border-border bg-background px-1 text-[11px]"
                              aria-label="Desde"
                              title="Solo mensajes desde esta fecha (vacío = todo)"
                              value={pick.since}
                              onChange={(e) => setPick(c.chatId, { since: e.target.value })}
                            />
                          </>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
              <div className="flex flex-wrap items-center justify-end gap-2">
                {confirmUnlink ? (
                  <button
                    type="button"
                    className={`${btnCls} border-status-error text-status-error`}
                    onClick={unlink}
                    disabled={busy}
                  >
                    <Unplug className="size-3" /> Sí, cerrar la sesión
                  </button>
                ) : (
                  <button type="button" className={btnCls} onClick={() => setConfirmUnlink(true)}>
                    <Unplug className="size-3" /> Desvincular
                  </button>
                )}
                <button
                  type="button"
                  className={btnCls}
                  onClick={async () => {
                    setBusy(true)
                    try {
                      await refreshStatus()
                      await loadChats()
                    } catch (e) {
                      toast.error(errMsg(e))
                    } finally {
                      setBusy(false)
                    }
                  }}
                  disabled={busy}
                >
                  <RefreshCw className={`size-3 ${busy ? "animate-spin" : ""}`} /> Refrescar
                </button>
                <button type="button" className={btnCls} onClick={save} disabled={busy}>
                  Guardar selección
                </button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  )
}
