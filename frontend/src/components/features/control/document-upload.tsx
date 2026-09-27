import { useRef, useState } from "react"
import { FileUp, Loader2 } from "lucide-react"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { StatusBadge } from "@/components/common/led"
import { ApiError, apiUpload } from "@/lib/api"

/** Lo que el backend acepta (espeja `documentos.ingest.BY_EXTENSION`). */
const ACEPTA = ".pdf,.zip,.png,.jpg,.jpeg,.webp"
const MAX_MB = 10

interface Subida {
  archivo: string
  estado: "subiendo" | "ok" | "duplicado" | "error"
  detalle: string
}

interface RespuestaUpload {
  inbox_id: number | null
  filename: string
  duplicado: boolean
  leido: boolean
  items: number
  cost_usd: number
  error?: string | null
}

function describir(r: RespuestaUpload): Subida {
  if (r.duplicado) {
    return { archivo: r.filename, estado: "duplicado", detalle: "ya lo tenías; no se duplicó" }
  }
  if (r.error) return { archivo: r.filename, estado: "error", detalle: r.error }
  if (!r.leido) {
    return { archivo: r.filename, estado: "error", detalle: "no se pudo leer el documento" }
  }
  const datos = r.items === 1 ? "1 dato" : `${r.items} datos`
  return { archivo: r.filename, estado: "ok", detalle: `leído · ${datos} extraídos` }
}

/**
 * Subir documentos desde el dashboard: extractos, facturas, contratos. Es la misma vía que el bot
 * de Telegram y `memex-documentos add`, así que subir acá lo que ya mandaste por Telegram no lo
 * duplica (la identidad es el contenido del archivo).
 *
 * Los archivos se suben de a uno a propósito: un extracto grande tarda decenas de segundos en
 * leerse y extraerse, y así se ve el progreso archivo por archivo en vez de un bloque opaco.
 */
export function DocumentUpload() {
  const input = useRef<HTMLInputElement>(null)
  const [subidas, setSubidas] = useState<Subida[]>([])
  const [subiendo, setSubiendo] = useState(false)

  async function onFiles(files: FileList | null) {
    if (!files || files.length === 0) return
    setSubiendo(true)
    for (const file of Array.from(files)) {
      if (file.size > MAX_MB * 1024 * 1024) {
        setSubidas((prev) => [
          { archivo: file.name, estado: "error", detalle: `pesa más de ${MAX_MB} MB` },
          ...prev,
        ])
        continue
      }
      setSubidas((prev) => [
        { archivo: file.name, estado: "subiendo", detalle: "leyendo…" },
        ...prev,
      ])
      const form = new FormData()
      form.append("archivo", file)
      form.append("procesar", "true")
      try {
        const r = await apiUpload<RespuestaUpload>("/documentos", form)
        setSubidas((prev) => [describir(r), ...prev.slice(1)])
      } catch (e) {
        const detalle = e instanceof ApiError ? e.message : "falló la subida"
        setSubidas((prev) => [{ archivo: file.name, estado: "error", detalle }, ...prev.slice(1)])
      }
    }
    setSubiendo(false)
    if (input.current) input.current.value = ""
  }

  return (
    <Panel>
      <PanelHeader
        eyebrow="carga · documentos"
        title="Subir un documento"
        sub="Extractos, facturas, contratos. Se lee el contenido y sale lo que tenga adentro (los movimientos de un extracto, por ejemplo)"
        right={<FileUp className="size-4 text-muted-foreground" />}
      />
      <PanelBody className="space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <input
            ref={input}
            type="file"
            multiple
            accept={ACEPTA}
            disabled={subiendo}
            onChange={(e) => void onFiles(e.target.files)}
            className="block w-full max-w-md text-sm file:mr-3 file:rounded-md file:border file:border-border file:bg-muted/40 file:px-3 file:py-1.5 file:text-sm hover:file:bg-muted disabled:opacity-50"
          />
          {subiendo && (
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" /> procesando…
            </span>
          )}
        </div>
        <p className="text-[11px] text-muted-foreground">
          PDF, ZIP o imagen, hasta {MAX_MB} MB. Un extracto grande puede tardar. Si el PDF tiene
          contraseña, debe estar cargada en memex. Subir dos veces el mismo archivo no lo duplica.
        </p>

        {subidas.length > 0 && (
          <ul className="space-y-1.5">
            {subidas.map((s, i) => (
              <li key={`${s.archivo}-${i}`} className="flex items-center justify-between gap-3 rounded-lg border border-border bg-background/40 px-3 py-2">
                <span className="truncate text-sm" title={s.archivo}>
                  {s.archivo}
                </span>
                <span className="flex shrink-0 items-center gap-2">
                  <span className="text-[11px] text-muted-foreground">{s.detalle}</span>
                  <StatusBadge
                    tone={
                      s.estado === "ok"
                        ? "ok"
                        : s.estado === "error"
                          ? "error"
                          : s.estado === "duplicado"
                            ? "neutral"
                            : "review"
                    }
                    label={s.estado}
                  />
                </span>
              </li>
            ))}
          </ul>
        )}
      </PanelBody>
    </Panel>
  )
}
