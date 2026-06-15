// Tarjeta "Cliente local" en /cuenta: la entrada VISIBLE para conectar el daemon que corre
// en la PC del usuario. El dashboard no puede arrancarlo (vive fuera del navegador) — lo que
// sí hace es decirte el comando exacto a pegar y mostrar si ya llegó algo de un cliente.

import { Cable, Check, Copy, Loader2 } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"
import { StatusBadge } from "@/components/common/led"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { fetchLocalClientStatus } from "@/data/accounts"
import { useAsync } from "@/lib/use-async"

/** URL sugerida del gateway/API para el comando. En dev: http://localhost:8787. */
function suggestedBaseUrl(): string {
  if (typeof window === "undefined") return "http://localhost:8787"
  const { protocol, hostname } = window.location
  return `${protocol}//${hostname}:8787`
}

function CopyableCommand({ cmd }: { cmd: string }) {
  const [copied, setCopied] = useState(false)
  async function copy(): Promise<void> {
    await navigator.clipboard.writeText(cmd)
    setCopied(true)
    toast.success("Comando copiado")
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div className="flex items-center gap-2 rounded-md border border-border bg-muted/30 px-2 py-1.5">
      <code className="num min-w-0 flex-1 truncate text-xs">{cmd}</code>
      <button
        type="button"
        onClick={copy}
        title="Copiar"
        className="shrink-0 text-muted-foreground transition-colors hover:text-foreground"
      >
        {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      </button>
    </div>
  )
}

export function LocalClientPanel() {
  const { data, loading } = useAsync(fetchLocalClientStatus)
  const base = suggestedBaseUrl()
  const sources = data?.sources ?? []

  return (
    <Panel className="lg:col-span-2">
      <PanelHeader
        eyebrow="cuenta · cliente local"
        title="Cliente local (ingesta desde tu PC)"
        sub="Un daemon que corre en TU computadora y empuja datos (Outlook de escritorio, IMAP universitario…) a memex. Se conecta por línea de comandos."
        right={<Cable className="size-4 text-muted-foreground" />}
      />
      <PanelBody className="space-y-3">
        <p className="text-xs text-muted-foreground">
          1. En tu PC, dentro del repo de memex, conectalo con un comando:
        </p>
        <CopyableCommand cmd={`uv run memex-local-client connect ${base}`} />
        <p className="text-xs text-muted-foreground">
          {data?.authEnforced
            ? "Este servidor pide autenticación: agregá --token <MEMEX_API_TOKEN> al comando."
            : "En dev no hace falta token. Si memex corre en otro equipo, ajustá la URL/puerto."}
        </p>
        <p className="text-xs text-muted-foreground">
          2. Instalá un plugin (guiado):{" "}
          <code className="num">uv run memex-local-client setup</code> — probá el caño con{" "}
          <code className="num">selftest</code>, o usá <code className="num">outlook-desktop</code> /{" "}
          <code className="num">imap-university</code>.
        </p>
        <p className="text-xs text-muted-foreground">
          3. Dejalo corriendo: <code className="num">daemon start</code> (o{" "}
          <code className="num">autostart enable</code> en Windows para que arranque solo).
        </p>

        <div className="border-t border-border pt-3">
          <div className="eyebrow mb-1.5">clientes detectados</div>
          {loading ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-3 animate-spin" /> cargando…
            </div>
          ) : sources.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              Todavía no llegó nada de un cliente local. Conectalo y corré el daemon.
            </p>
          ) : (
            <ul className="space-y-1">
              {sources.map((s) => (
                <li
                  key={s.sourceId}
                  className="flex items-center justify-between gap-2 text-xs"
                >
                  <span className="num min-w-0 truncate">{s.name}</span>
                  <StatusBadge tone="ok" label={s.type} />
                </li>
              ))}
            </ul>
          )}
        </div>
      </PanelBody>
    </Panel>
  )
}
