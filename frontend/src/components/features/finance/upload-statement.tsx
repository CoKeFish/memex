// Subir el extracto del banco desde la propia vista de finanzas. Es el arreglo de lo que el aviso
// de cobertura señala: un mes sin extracto solo tiene los cobros que llegaron por correo. El archivo
// entra por `POST /documentos` (el mismo camino que la consola y Telegram): se lee, se extraen los
// movimientos y se deduplican contra los que ya estaban.

import { useRef, useState } from "react"
import { toast } from "sonner"
import { Loader2, Upload } from "lucide-react"
import { ApiError } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { uploadDocumento } from "@/data"

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e)
}

export function UploadStatement({ onDone }: { onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const input = useRef<HTMLInputElement | null>(null)

  async function send(files: FileList | null): Promise<void> {
    if (!files || files.length === 0) return
    setBusy(true)
    try {
      for (const f of Array.from(files)) {
        const r = await uploadDocumento(f)
        if (r.duplicado) toast.info(`${f.name}: ese extracto ya estaba`)
        else if (r.error) toast.warning(`${f.name}: ${r.error}`)
        else toast.success(`${f.name}: ${r.items} movimientos leídos`)
      }
      onDone()
    } catch (e) {
      toast.error(`Subir extracto: ${errMsg(e)}`)
    } finally {
      setBusy(false)
      if (input.current) input.current.value = ""
    }
  }

  return (
    <>
      <input
        ref={input}
        type="file"
        multiple
        accept=".pdf,.zip,.csv,.png,.jpg,.jpeg"
        className="hidden"
        onChange={(e) => void send(e.target.files)}
      />
      <Button
        size="sm"
        variant="outline"
        disabled={busy}
        onClick={() => input.current?.click()}
        title="El PDF del banco: se leen sus movimientos y se deduplican contra los que ya están"
      >
        {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Upload className="size-3.5" />}
        {busy ? "Leyendo…" : "Subir extracto"}
      </Button>
    </>
  )
}
