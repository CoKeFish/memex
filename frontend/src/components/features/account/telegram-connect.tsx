// Wizard "Conectar Telegram" + selector de grupos/canales (un Dialog, multi-paso).
// Login: pedir código → ingresar código → [contraseña 2FA]. Luego: discover + multi-select de chats
// que se persisten en `allowed_chats` del source vinculado. Los supergrupos con TEMAS (forum) se
// expanden para elegir sub-canales puntuales (topic_ids). Credenciales server-side (vault).

import { Loader2, Plug, RefreshCw } from "lucide-react"
import { type FormEvent, useState } from "react"
import { toast } from "sonner"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  type AllowedChatInput,
  discoverTelegramChats,
  discoverTelegramTopics,
  getTelegramSource,
  type ManagedAccount,
  requestTelegramCode,
  setAllowedChats,
  submitTelegramCode,
  submitTelegramPassword,
  type TelegramChat,
  type TelegramTopic,
} from "@/data/accounts"
import { ApiError } from "@/lib/api"

const inputCls =
  "rounded-md border border-border bg-background px-2 py-1.5 text-sm outline-none focus:border-brand"
const btnCls =
  "inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs hover:bg-accent/40 disabled:opacity-50"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : "Algo salió mal"
}

type Step = "code" | "2fa" | "chats"

export function TelegramConnect({
  account,
  onDone,
}: {
  account: ManagedAccount
  onDone: () => void | Promise<void>
}) {
  const [open, setOpen] = useState(false)
  const [step, setStep] = useState<Step>("code")
  const [busy, setBusy] = useState(false)
  const [state, setState] = useState("")
  const [phoneMasked, setPhoneMasked] = useState("")
  const [code, setCode] = useState("")
  const [password, setPassword] = useState("")
  const [sourceId, setSourceId] = useState<number | null>(null)
  const [baseConfig, setBaseConfig] = useState<Record<string, unknown>>({})
  const [chats, setChats] = useState<TelegramChat[]>([])
  // chatId → topics específicos elegidos ([] = todos los temas del chat). Presencia = chat incluido.
  const [selected, setSelected] = useState<Map<number, number[]>>(new Map())
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const [topicsByChat, setTopicsByChat] = useState<Map<number, TelegramTopic[]>>(new Map())
  const [loadingTopics, setLoadingTopics] = useState<Set<number>>(new Set())

  async function loadChats(): Promise<void> {
    const [src, discovered] = await Promise.all([
      getTelegramSource(account.id),
      discoverTelegramChats(account.id),
    ])
    setChats(discovered)
    if (src) {
      setSourceId(src.sourceId)
      setBaseConfig(src.config)
      setSelected(new Map(src.allowedChats.map((c) => [c.chatId, c.topicIds ?? []])))
    }
  }

  async function start(): Promise<void> {
    setStep("code")
    setState("")
    setCode("")
    setPassword("")
    setChats([])
    setSelected(new Map())
    setExpanded(new Set())
    setTopicsByChat(new Map())
    setSourceId(null)
    setOpen(true)
    setBusy(true)
    try {
      const r = await requestTelegramCode(account.id)
      setPhoneMasked(r.phoneMasked)
      if (r.alreadyAuthorized) {
        await loadChats()
        setStep("chats")
      } else {
        setState(r.state)
        setStep("code")
      }
    } catch (e) {
      toast.error(errMsg(e))
      setOpen(false)
    } finally {
      setBusy(false)
    }
  }

  async function onSubmitCode(ev: FormEvent): Promise<void> {
    ev.preventDefault()
    setBusy(true)
    try {
      const r = await submitTelegramCode(account.id, state, code.trim())
      if (r === "2fa_required") {
        setStep("2fa")
      } else {
        await loadChats()
        setStep("chats")
        toast.success("Telegram conectado")
      }
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  async function onSubmitPassword(ev: FormEvent): Promise<void> {
    ev.preventDefault()
    setBusy(true)
    try {
      await submitTelegramPassword(account.id, state, password)
      await loadChats()
      setStep("chats")
      toast.success("Telegram conectado")
    } catch (e) {
      toast.error(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  function toggleChat(chatId: number): void {
    setSelected((prev) => {
      const next = new Map(prev)
      if (next.has(chatId)) next.delete(chatId)
      else next.set(chatId, [])
      return next
    })
  }

  function toggleTopic(chatId: number, topicId: number): void {
    setSelected((prev) => {
      const next = new Map(prev)
      const cur = next.get(chatId) ?? []
      next.set(
        chatId,
        cur.includes(topicId) ? cur.filter((t) => t !== topicId) : [...cur, topicId],
      )
      return next
    })
  }

  async function expandChat(chatId: number): Promise<void> {
    const isExpanding = !expanded.has(chatId)
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(chatId)) next.delete(chatId)
      else next.add(chatId)
      return next
    })
    if (isExpanding && !topicsByChat.has(chatId) && !loadingTopics.has(chatId)) {
      setLoadingTopics((prev) => new Set(prev).add(chatId))
      try {
        const topics = await discoverTelegramTopics(account.id, chatId)
        setTopicsByChat((prev) => new Map(prev).set(chatId, topics))
      } catch (e) {
        toast.error(errMsg(e))
      } finally {
        setLoadingTopics((prev) => {
          const next = new Set(prev)
          next.delete(chatId)
          return next
        })
      }
    }
  }

  async function saveChats(): Promise<void> {
    if (sourceId == null) {
      toast.error("Vinculá una source de Telegram a la cuenta primero")
      return
    }
    setBusy(true)
    try {
      const picks: AllowedChatInput[] = [...selected].map(([chatId, topicArr]) => ({
        chatId,
        topicIds: topicArr.length ? topicArr : null,
      }))
      await setAllowedChats(sourceId, picks, baseConfig)
      toast.success(`${picks.length} chat(s) guardados`)
      setOpen(false)
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
        <Plug className="size-3" /> Conectar / Chats
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Telegram · {account.alias}</DialogTitle>
            <DialogDescription>
              {step === "code" && `Te enviamos un código a ${phoneMasked}. Ingresalo abajo.`}
              {step === "2fa" &&
                "Tu cuenta tiene verificación en dos pasos. Ingresá tu contraseña."}
              {step === "chats" && "Elegí los grupos/canales (y temas) a ingerir."}
            </DialogDescription>
          </DialogHeader>

          {busy && step !== "chats" && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-3 animate-spin" /> trabajando…
            </div>
          )}

          {step === "code" && (
            <form onSubmit={onSubmitCode} className="flex items-end gap-2">
              <input
                className={`${inputCls} w-40`}
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="código"
                inputMode="numeric"
                autoFocus
              />
              <button type="submit" disabled={busy || !code.trim()} className={btnCls}>
                Enviar
              </button>
            </form>
          )}

          {step === "2fa" && (
            <form onSubmit={onSubmitPassword} className="flex items-end gap-2">
              <input
                type="password"
                className={`${inputCls} w-48`}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="contraseña 2FA"
                autoComplete="off"
                autoFocus
              />
              <button type="submit" disabled={busy || !password} className={btnCls}>
                Enviar
              </button>
            </form>
          )}

          {step === "chats" && (
            <div className="space-y-2">
              {chats.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  Sin grupos/canales accesibles (¿la cuenta está en alguno?).
                </p>
              ) : (
                <div className="max-h-72 space-y-1 overflow-y-auto rounded-md border border-border p-2">
                  {chats.map((c) => {
                    const topicSel = selected.get(c.chatId) ?? []
                    const isExpanded = expanded.has(c.chatId)
                    const topics = topicsByChat.get(c.chatId) ?? []
                    return (
                      <div key={c.chatId}>
                        <div className="flex items-center gap-2 text-sm">
                          <input
                            type="checkbox"
                            checked={selected.has(c.chatId)}
                            onChange={() => toggleChat(c.chatId)}
                          />
                          <span className="truncate">{c.name || "(sin nombre)"}</span>
                          <span className="eyebrow text-muted-foreground">{c.kind}</span>
                          {c.isForum && (
                            <button
                              type="button"
                              className="ml-auto text-xs text-muted-foreground hover:text-foreground"
                              onClick={() => expandChat(c.chatId)}
                            >
                              {isExpanded ? "▾ temas" : "▸ temas"}
                              {topicSel.length ? ` (${topicSel.length})` : ""}
                            </button>
                          )}
                        </div>
                        {c.isForum && isExpanded && (
                          <div className="mt-1 ml-6 space-y-0.5 border-l border-border pl-2">
                            {loadingTopics.has(c.chatId) ? (
                              <p className="text-[11px] text-muted-foreground">cargando temas…</p>
                            ) : topics.length === 0 ? (
                              <p className="text-[11px] text-muted-foreground">
                                sin temas (o sin permiso)
                              </p>
                            ) : (
                              <>
                                <p className="text-[11px] text-muted-foreground">
                                  {topicSel.length
                                    ? "solo estos temas:"
                                    : "todos los temas (marcá para acotar)"}
                                </p>
                                {topics.map((t) => (
                                  <label
                                    key={t.topicId}
                                    className="flex items-center gap-2 text-xs"
                                  >
                                    <input
                                      type="checkbox"
                                      checked={topicSel.includes(t.topicId)}
                                      onChange={() => toggleTopic(c.chatId, t.topicId)}
                                    />
                                    <span className="truncate">{t.title}</span>
                                  </label>
                                ))}
                              </>
                            )}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
              <div className="flex items-center justify-end gap-2">
                <button type="button" className={btnCls} onClick={loadChats} disabled={busy}>
                  <RefreshCw className={`size-3 ${busy ? "animate-spin" : ""}`} /> Refrescar
                </button>
                <button
                  type="button"
                  className={btnCls}
                  onClick={saveChats}
                  disabled={busy || sourceId == null}
                >
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
