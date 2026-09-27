# Módulo `identidades` — pipeline actual

> Estado a 2026-06-21 (main `34ea048`). Describe cómo procesa el módulo HOY, end-to-end.

## Qué hace el módulo
Mantiene un **directorio de identidades** (personas, organizaciones, productos) a partir de los
mensajes ingeridos (correo, chat, social). Principio rector:

- Una **identidad** = nombre + alias + **identificadores** (email/dominio/teléfono/handle) +
  posición en la **jerarquía** (pertenece-a) + **afiliaciones** (persona↔org con rol).
- Un **identificador es un ATRIBUTO de UNA identidad, nunca una identidad**. Un email/dominio
  pertenece a exactamente una identidad.

## Modelo de datos (tablas)
| Tabla | Qué guarda |
|---|---|
| `mod_identidades` | la ficha: `kind` (persona/organizacion/producto/desconocido), `display_name`, `aliases`, `parent_identity_id` (jerarquía), `source`, `metadata` |
| `mod_identidades_identifiers` | atributos: `kind` (email/domain/phone/handle), `value_norm`. Índice ÚNICO parcial `(user,kind,value_norm)` para email/phone/domain → cada uno pertenece a UNA ficha |
| `mod_identidades_mentions` | **avistamientos**: "tal identidad apareció en tal(es) correo(s)". `source_inbox_ids`, `resolved_identity_id`, `resolution_method` (sender/exact_name/fuzzy/created…), `email` |
| `mod_identidades_person_orgs` | **afiliaciones**: `person_id` → `org_id` con `role` (muchos-a-muchos) |
| `identidades_resolver_settings` | perillas: `resolver_enabled`, `batch_maintenance_enabled`, umbrales de confianza |

**Jerarquía vs afiliación** (no confundir):
- **Jerarquía** (`parent_identity_id`, árbol, un solo padre): SOLO org/producto. "X es SUB-PARTE de
  Y" (carrera→universidad). Una persona NUNCA va en la jerarquía.
- **Afiliación** (`person_orgs`, m-a-m, con rol): persona↔org. "X es MIEMBRO de Y" (Valeria→RAS
  [Presidenta]).
- Una org puede estar en ambas (RAS cuelga de la U **y** tiene afiliados). Un par concreto es de un
  solo tipo: persona↔org = afiliación; org↔org = jerarquía.

## La pipeline (orquestador, `modules/orchestrator.py`)

La ingesta deja mensajes en `inbox`. El **gate de relevancia** (módulo aparte) los clasifica; el
**work-set** ya viene filtrado (sin blacklist, solo lo relevante). El orquestador agrupa en
**ventanas por CANTIDAD** (topes por fase: relevancia 40, extracción 10, resumen 20) y procesa cada
ventana con `_process_window`. Por ventana, en orden:

### 1. Remitente — `weave_sender_structure` (determinista, Paso 5)
Corre SIEMPRE (independiente del ruteo LLM): el remitente es estructura del mensaje, no algo a
extraer. Asimétrico por medio:
- **chat:** el remitente es una PERSONA → se crea + su `platform_id`/handle.
- **email:** `_resolve_email_sender` (ver detalle abajo).
- **social:** la cuenta por handle → `desconocido` si es nueva.

Inserta una **mención** `resolution_method='sender'` (el avistamiento del remitente).

### 2. Ruteo + Extracción + Persist/dedup
- **Ruteo (Etapa A):** un LLM decide qué módulos procesan cada mensaje (`routing_enabled`).
- **Extracción (Fase 1, `_extract_unit`):** concurrente, SIN efectos. El LLM lee el mensaje
  renderizado (`render_payload`: `Nombre <email>: Asunto…\ncuerpo`) y devuelve menciones de
  identidad (nombre + tipo + identificadores que aparezcan).
- **Persist/dedup (Fase 2, `_persist_unit`):** secuencial, en orden topológico. **Solo LOOKUP**
  (procedimental, sin LLM): ¿el identificador exacto (email/dominio/handle) ya pertenece a una
  ficha? ¿se parece por difuso (alias)? Si resuelve → atribuye a la existente. **No inventa
  identidades nuevas para lo ambiguo** (eso es del resolver).

### 3. Resolver contextual — `run_resolver_window` (LLM, gated `resolver_enabled`)
Por cada correo que lo necesite (`email_needs_resolution` = hay algo sin resolver), arma el
contexto (`build_email_context`) y llama UNA vez al LLM. El contexto lleva **las 3 partes del
correo**: REMITENTE (email), ASUNTO, CUERPO; + las identidades del correo (con sus datos + padre +
hijos) + CANDIDATAS del directorio (trigrama + **por dominio del remitente**, con sus ancestros).

El LLM decide **3 cosas** (`prompt.py::IDENTIDADES_RESOLVE_SYSTEM_PROMPT`):
1. **FUSIONES** — qué fichas son la MISMA entidad (`keep_id`/`drop_id`).
2. **RELACIONES de pertenencia** — `source` pertenece/es-miembro de `target` (siempre una org). El
   sistema DEDUCE el tipo por kind: persona→org = **afiliación**; org/producto→org = **jerarquía**.
   (`target_name` si el padre debería existir pero no está en las listas.)
3. **REMITENTE** — el email del remitente ¿es un BUZÓN de una org (`owner_id`) o una PERSONA
   (`person_name`)?

`apply_resolution` aplica en orden: merges → jerarquía → disposición del remitente → afiliaciones →
marca el correo resuelto (skip incremental la próxima vez).

### 4. Atribución de dominio — `attribute_domains_for_window` (LLM, gated `resolver_enabled`)
Corre tras el resolver. Para cada dominio corporativo del remitente que quedó **sin dueña**, el LLM
elige —entre las orgs que CO-OCURREN con el dominio **+ sus ancestros en la jerarquía**— cuál es la
dueña, prefiriendo la org de **más alto nivel** (la institución, no una sub-unidad). Si hay una
clara: le cuelga el `domain` + re-teje los remitentes de ese dominio. NO crea orgs (solo elige entre
existentes).

### 5. Relaciones / co-ocurrencia (módulo `relations`, aparte)
Las menciones (incluida la del remitente) co-ocurren → aristas del grafo (tejido incremental +
reconcile). Fuera del alcance de este doc.

## El camino del remitente de EMAIL — `_resolve_email_sender` (detalle)
Política domain-agnóstica, SIN crear stubs de email/persona (un email es atributo, no identidad):
1. **email exacto ya conocido** (no-rol) → esa identidad.
2. **free-mail** (gmail/outlook…) → None (leftover: lo decide el resolver).
3. **dominio corporativo** (`_org_for_domain`):
   - el dominio YA pertenece a una org → esa org;
   - rol/relay CON nombre de org (`noreply@nequi` "Nequi") → crea por NOMBRE + ata el dominio;
   - corporativo sin org real conocida → None (leftover).
   - si hay org y el email es no-rol → además cuelga el email como CONTACTO de la org.

> **UN DOMINIO NO ES UNA IDENTIDAD:** ya NO se crea una ficha nombrada por el dominio (el viejo
> "#1", eliminado). El dominio se ata a la org real por nombre (rol/relay) o a mano vía
> `attribute_domain`.

## Perillas / gates
- `resolver_enabled` — gatea el resolver contextual Y `attribute_domain` (corren juntos).
- `routing_enabled` — gatea el ruteo LLM (OFF = todos los módulos procesan todo).
- `batch_maintenance_enabled` — organize + merge phase-2 por lotes (scheduler).
- El **daemon/scheduler** arranca apagado; la cadencia temporal es suya, no del ventaneo.

## Temas abiertos / en discusión
- **Atribución de email corporativo (en revisión):** hoy, si el dominio ya tiene dueña, un email
  corporativo no-rol (`correo_cs92pro@`) se resuelve y cuelga **procedimentalmente** en la dueña del
  dominio. El dueño pidió que eso NO lo decida el código: el email nuevo debe quedar leftover y que
  **el LLM elija** a qué identidad pertenece (con el contexto/firma del cuerpo).
- **Clasificador codex+web (pendiente):** para tipar `desconocido` y crear orgs por nombre desde el
  dominio cuando no hay candidata existente (lo que `attribute_domain` no hace).
