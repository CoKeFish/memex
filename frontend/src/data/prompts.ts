// Prompts editables (router /prompts). El código es el default; un override guardado desde acá
// se aplica en todos los procesos en menos de un minuto (ver backend `memex.prompts`).
import { apiDelete, apiGet, apiPost, apiPut } from "@/lib/api"

export interface PromptSummary {
  key: string
  title: string
  group: string
  description: string
  consumer: string
  editable: boolean
  hasOverride: boolean
  note: string
  updatedAt: string | null
  defaultChars: number
  effectiveChars: number
}

export interface PromptVersion {
  id: number
  note: string
  createdAt: string
  chars: number
  text: string
}

export interface PromptDetail extends PromptSummary {
  /** Texto del código. */
  default: string
  /** Texto guardado desde la UI; null = rige el código. */
  override: string | null
  /** Lo que un consumidor leería ahora mismo en el proceso de la API. */
  effective: string
  warnings: string[]
  versions: PromptVersion[]
}

export interface PromptList {
  groups: string[]
  items: PromptSummary[]
}

interface PromptSummaryApi {
  key: string
  title: string
  group: string
  description: string
  consumer: string
  editable: boolean
  has_override: boolean
  note: string
  updated_at: string | null
  default_chars: number
  effective_chars: number
}

interface PromptVersionApi {
  id: number
  note: string
  created_at: string
  chars: number
  text: string
}

interface PromptDetailApi extends PromptSummaryApi {
  default: string
  override: string | null
  effective: string
  warnings: string[]
  versions: PromptVersionApi[]
}

export function toPromptSummary(a: PromptSummaryApi): PromptSummary {
  return {
    key: a.key,
    title: a.title,
    group: a.group,
    description: a.description,
    consumer: a.consumer,
    editable: a.editable,
    hasOverride: a.has_override,
    note: a.note,
    updatedAt: a.updated_at,
    defaultChars: a.default_chars,
    effectiveChars: a.effective_chars,
  }
}

export function toPromptDetail(a: PromptDetailApi): PromptDetail {
  return {
    ...toPromptSummary(a),
    default: a.default,
    override: a.override,
    effective: a.effective,
    warnings: a.warnings ?? [],
    versions: (a.versions ?? []).map((v) => ({
      id: v.id,
      note: v.note,
      createdAt: v.created_at,
      chars: v.chars,
      text: v.text,
    })),
  }
}

export async function fetchPrompts(): Promise<PromptList> {
  const r = await apiGet<{ groups: string[]; items: PromptSummaryApi[] }>("/prompts")
  return { groups: r.groups, items: r.items.map(toPromptSummary) }
}

export async function fetchPrompt(key: string): Promise<PromptDetail> {
  return toPromptDetail(await apiGet<PromptDetailApi>(`/prompts/${encodeURIComponent(key)}`))
}

export async function savePrompt(key: string, text: string, note: string): Promise<PromptDetail> {
  return toPromptDetail(
    await apiPut<PromptDetailApi>(`/prompts/${encodeURIComponent(key)}`, { text, note }),
  )
}

export async function resetPrompt(key: string): Promise<PromptDetail> {
  return toPromptDetail(await apiDelete<PromptDetailApi>(`/prompts/${encodeURIComponent(key)}`))
}

export async function revertPrompt(key: string, versionId: number): Promise<PromptDetail> {
  return toPromptDetail(
    await apiPost<PromptDetailApi>(`/prompts/${encodeURIComponent(key)}/revert/${versionId}`),
  )
}
