// Referencia in-page de la vista /filtros: dónde corta cada mecanismo de filtrado, el mini-DSL
// del scope de filter_rules y los atributos filtrables por tipo de fuente (espejo curado de
// core/payloads.py — la paridad la vigila tests/test_filter_attributes_parity.py). Estático,
// sin fetch; colapsado por default para no empujar la gestión.

import { useState, type ReactNode } from "react"
import { ChevronDown, ChevronUp, Copy } from "lucide-react"
import { Link } from "react-router-dom"
import { toast } from "sonner"
import { Panel, PanelBody, PanelHeader } from "@/components/common/panel"
import { Button } from "@/components/ui/button"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { FILTER_PAYLOAD_DOCS, OPERATOR_DOCS } from "@/lib/filter-attributes"

function DocLink({ to, children }: { to: string; children: string }) {
  return (
    <Link to={to} className="underline underline-offset-2 hover:text-primary">
      {children}
    </Link>
  )
}

/** Ejemplo de scope copiable. */
function ScopeExample({ scope }: { scope: string }) {
  return (
    <span className="inline-flex max-w-full items-center gap-1">
      <code className="num min-w-0 truncate rounded bg-muted/60 px-1 py-px text-[10px]" title={scope}>
        {scope}
      </code>
      <button
        type="button"
        className="shrink-0 text-muted-foreground transition-colors hover:text-foreground"
        title="Copiar ejemplo"
        onClick={() => {
          void navigator.clipboard.writeText(scope)
          toast.success("Ejemplo copiado", { description: scope })
        }}
      >
        <Copy className="size-3" />
      </button>
    </span>
  )
}

function SectionTitle({ children }: { children: string }) {
  return <h3 className="eyebrow">{children}</h3>
}

/** Las 7 etapas del pipeline de filtrado, en orden, con su superficie de gestión. */
function PipelineSection() {
  const stages: { name: string; detail: ReactNode }[] = [
    {
      name: "Reglas pre-ingest (filter_rules)",
      detail: (
        <>
          Drop puro ANTES de guardar: lo que matchea una regla <code>ignore</code> no entra a la
          bandeja (solo queda un contador en logs). Se gestionan en esta página, arriba.
        </>
      ),
    },
    {
      name: "Clasificación por tier (heurística determinista)",
      detail: (
        <>
          Al clasificar, cada mensaje recibe un tier sin LLM: <code>list_id</code> presente,{" "}
          <code>list_unsubscribe</code> presente, <code>precedence</code> ∈ bulk/list/junk o{" "}
          <code>auto_submitted</code> ≠ &quot;no&quot; → <strong>Lista negra</strong> (se guarda,
          sin gasto LLM); si nada matchea → <strong>Lote</strong>. <strong>Individual</strong> no
          se asigna por heurística: solo por override o a mano.
        </>
      ),
    },
    {
      name: "Tier por remitente (override)",
      detail: (
        <>
          Fuerza el tier de los mensajes futuros de un remitente y le gana a la heurística. Se
          gestiona en esta página (sección de arriba) y desde{" "}
          <DocLink to="/relevancia">Relevancia</DocLink>.
        </>
      ),
    },
    {
      name: "Tier manual por mensaje",
      detail: (
        <>
          En el detalle de un mensaje en <DocLink to="/datos">Datos</DocLink> (menú
          &quot;Filtros&quot;): reasignar tier, bloquear remitente o re-clasificar.
        </>
      ),
    },
    {
      name: "Ruteo por módulo (consumes_kinds)",
      detail: (
        <>
          Determinista: cada módulo de extracción solo procesa sus categorías de fuente
          (correo/chat/social). Cobertura por módulo en{" "}
          <DocLink to="/procesamiento">Procesamiento</DocLink>.
        </>
      ),
    },
    {
      name: "Allowlist social",
      detail: (
        <>
          De las redes solo se traen las cuentas configuradas por fuente
          (<code>config.accounts</code>); se ven en <DocLink to="/carga">Carga / ingesta</DocLink>.
        </>
      ),
    },
    {
      name: "Relevancia y candidatos",
      detail: (
        <>
          Métrica determinista por remitente + cola de candidatos a filtrar (ruido detectado) en{" "}
          <DocLink to="/relevancia">Relevancia</DocLink>; de ahí salen las acciones
          &quot;no procesar&quot; (override) y &quot;descartar&quot; (regla pre-ingest).
        </>
      ),
    },
  ]
  return (
    <div className="space-y-2">
      <SectionTitle>dónde corta cada mecanismo</SectionTitle>
      <ol className="space-y-1.5">
        {stages.map((s, i) => (
          <li key={s.name} className="flex gap-2 text-xs">
            <span className="num shrink-0 text-muted-foreground">{i + 1}.</span>
            <span className="min-w-0">
              <span className="font-medium">{s.name}.</span>{" "}
              <span className="text-muted-foreground">{s.detail}</span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  )
}

function DslSection() {
  return (
    <div className="space-y-2">
      <SectionTitle>mini-DSL del scope (reglas pre-ingest)</SectionTitle>
      <ul className="list-disc space-y-1 pl-4 text-xs text-muted-foreground">
        <li>
          Las keys del scope son paths del payload; los anidados van con dot-notation
          (<code>from.email</code>, <code>sender.username</code>).
        </li>
        <li>Varias keys en un scope = AND: todas deben matchear.</li>
        <li>
          Las reglas se evalúan por prioridad descendente y la <strong>primera</strong> que matchea
          decide; <code>keep</code> con prioridad alta sirve como excepción a un{" "}
          <code>ignore</code> más general.
        </li>
        <li>
          Acciones: <code>ignore</code> = drop puro · <code>keep</code> = pasa explícito ·{" "}
          <code>archive</code> = previsto, hoy equivale a keep.
        </li>
        <li>
          Scope vacío <code>{"{}"}</code> matchea todo (combinado con <code>source_type</code>{" "}
          corta una fuente entera). <code>source_type</code> vacío = aplica a todas las fuentes.
        </li>
      </ul>
      <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full text-xs">
          <thead className="bg-muted/50 text-left text-[11px] text-muted-foreground">
            <tr>
              <th className="px-2.5 py-1.5 font-medium">Operador</th>
              <th className="px-2.5 py-1.5 font-medium">Forma</th>
              <th className="px-2.5 py-1.5 font-medium">Qué hace</th>
              <th className="px-2.5 py-1.5 font-medium">Ejemplo</th>
            </tr>
          </thead>
          <tbody>
            {OPERATOR_DOCS.map((o) => (
              <tr key={o.op} className="border-t border-border align-top">
                <td className="num px-2.5 py-1.5 font-medium">{o.op}</td>
                <td className="num px-2.5 py-1.5 text-muted-foreground">{o.signature}</td>
                <td className="px-2.5 py-1.5 text-muted-foreground">{o.description}</td>
                <td className="px-2.5 py-1.5">
                  <ScopeExample scope={o.example} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function AttributesSection() {
  return (
    <div className="space-y-2">
      <SectionTitle>atributos por tipo de fuente</SectionTitle>
      <Tabs defaultValue="email">
        <TabsList>
          {FILTER_PAYLOAD_DOCS.map((d) => (
            <TabsTrigger key={d.kind} value={d.kind} className="text-xs">
              {d.label}
            </TabsTrigger>
          ))}
        </TabsList>
        {FILTER_PAYLOAD_DOCS.map((d) => (
          <TabsContent key={d.kind} value={d.kind} className="space-y-2">
            <p className="text-[11px] text-muted-foreground">
              source_type: {d.sourceTypes.map((t) => <code key={t} className="mr-1">{t}</code>)}
            </p>
            <div className="overflow-x-auto rounded-md border border-border">
              <table className="w-full text-xs">
                <thead className="bg-muted/50 text-left text-[11px] text-muted-foreground">
                  <tr>
                    <th className="px-2.5 py-1.5 font-medium">Atributo</th>
                    <th className="px-2.5 py-1.5 font-medium">Tipo</th>
                    <th className="px-2.5 py-1.5 font-medium">Descripción</th>
                    <th className="px-2.5 py-1.5 font-medium">Ejemplo</th>
                  </tr>
                </thead>
                <tbody>
                  {d.attributes.map((a) => (
                    <tr key={a.path} className="border-t border-border align-top">
                      <td className="num whitespace-nowrap px-2.5 py-1.5 font-medium">{a.path}</td>
                      <td className="num px-2.5 py-1.5 text-muted-foreground">{a.type}</td>
                      <td className="px-2.5 py-1.5 text-muted-foreground">{a.description}</td>
                      <td className="px-2.5 py-1.5">
                        {a.matchable ? (
                          a.example ? (
                            <ScopeExample scope={a.example} />
                          ) : null
                        ) : (
                          <span
                            className="text-[10px] text-muted-foreground/70"
                            title="El DSL v1 no matchea arrays por elemento."
                          >
                            no matcheable
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {d.notes.length > 0 && (
              <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-muted-foreground">
                {d.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            )}
          </TabsContent>
        ))}
      </Tabs>
    </div>
  )
}

function LimitationsSection() {
  return (
    <div className="space-y-2">
      <SectionTitle>limitaciones</SectionTitle>
      <ul className="list-disc space-y-1 pl-4 text-xs text-muted-foreground">
        <li>
          Los arrays (<code>to</code>, <code>cc</code>, <code>flags</code>,{" "}
          <code>references</code>, <code>attachments</code>, <code>media_refs</code>) no se
          matchean por elemento en el DSL v1.
        </li>
        <li>
          <code>regex</code> y <code>prefix</code> solo aplican a strings; números y booleanos van
          con <code>equals</code> / <code>in</code>.
        </li>
        <li>
          Si un objeto anidado es null (<code>sender</code> en service messages,{" "}
          <code>engagement</code> sin datos), sus sub-paths no resuelven y esa key no matchea.
        </li>
        <li>
          Todo es prospectivo: reglas y overrides afectan lo que llega o se clasifica DESPUÉS, no
          lo ya recibido (para eso está el reproceso en el detalle del mensaje).
        </li>
      </ul>
    </div>
  )
}

export function FiltersDocs() {
  const [open, setOpen] = useState(false)
  return (
    <Panel className="overflow-hidden">
      <PanelHeader
        eyebrow="filtros · referencia"
        title="Cómo se filtra y qué se puede filtrar"
        sub="el pipeline completo, el mini-DSL del scope y los atributos disponibles por tipo de fuente"
        right={
          <Button variant="outline" size="sm" className="h-8" onClick={() => setOpen((v) => !v)}>
            {open ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />}
            {open ? "Cerrar" : "Abrir"}
          </Button>
        }
      />
      {open && (
        <PanelBody className="space-y-5">
          <PipelineSection />
          <DslSection />
          <AttributesSection />
          <LimitationsSection />
        </PanelBody>
      )}
    </Panel>
  )
}
