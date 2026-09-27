# Módulo eventos (calendario) — arquitectura

> En el código el módulo se llama **`calendar`** (`src/memex/modules/calendar/`). Es el dominio de
> **eventos con fecha** de memex.

Junta tus eventos de **dos mundos** y los deja como un único calendario limpio:
- los que **extrae de tus mensajes** (una cita en un correo, un vuelo, un vencimiento), y
- los que **sincroniza de tu calendario externo** (Google/Outlook).

Y es **bidireccional**: no solo lee tu calendario externo — también **escribe de vuelta** tu vista
consolidada (el único dato que *sale* de memex hacia afuera).

Su problema central es el mismo que finanzas: **el mismo evento llega por varios lados** (el invite por
correo + el evento ya en tu Google). Por eso deduplica y **consolida** en un solo evento real.

## Arquitectura

```mermaid
flowchart TB
  ext["Mensajes (extracción)<br/>citas · vuelos · vencimientos"]
  prov["GOOGLE / OUTLOOK<br/>tu calendario externo"]
  mod["Otros módulos<br/>(proponen eventos)"]

  subgraph cal["MÓDULO CALENDAR"]
    cruda[("EVENTOS CRUDOS<br/>cada fuente tal como llega")]
    dedup{"DEDUP<br/>¿son el mismo evento?"}
    cons[("EVENTO CONSOLIDADO<br/>uno por evento real (= el vértice)")]
  end

  ext --> cruda
  prov -. "trae (pull)" .-> cruda
  mod --> cruda
  cruda --> dedup
  dedup --> cons
  cons -. "escribe de vuelta (write-back)" .-> prov
  cons --> vista["Vista /calendar + grafo"]

  classDef store fill:#fde68a,stroke:#b45309,color:#1f2937;
  class cruda,cons store;
```

**De un vistazo:** un evento entra por **tres lados** (mensajes, tu calendario externo, otros
módulos) y todos caen en la **misma tabla de crudos**. El **dedup** decide cuáles son el mismo evento;
la **consolidación** los funde en uno real (el que ves y el nodo del grafo). Con tu calendario externo
la flecha va en **los dos sentidos**: lo trae (*pull*) y le escribe de vuelta (*write-back*).

## Responsabilidades

1. **Extraer eventos de mensajes** — citas, reuniones, clases, exámenes, vuelos, vencimientos. Solo
   eventos reales con fecha concreta (no promos).
2. **Traer tu calendario externo** — sincroniza eventos de Google/Outlook **directo**, sin pasar por
   el pipeline de mensajes.
3. **Aceptar eventos de otros módulos** — un módulo puede proponer eventos (vía implementada, hoy sin
   uso).
4. **Deduplicar** — reconocer cuándo dos avisos son el mismo evento (se **solapan en el tiempo** +
   título o lugar **parecido**). Lo marca conservador; la IA confirma los dudosos.
5. **Consolidar** — fundir los duplicados en **un** evento real, eligiendo un "ganador" por prioridad.
6. **Prioridad y conflictos** — lo **manual** (o de tu calendario) gana; dos eventos importantes que
   **chocan de horario** quedan marcados para que **vos** revises (nunca los fusiona a la fuerza).
7. **Escribir de vuelta (write-back)** — empuja tu vista consolidada de regreso a Google/Outlook (lo
   único que *sale* de memex), sin entrar en bucle.
8. **Servir** — la vista `/calendar`, el grafo (cada evento es un nodo), y responder a otros módulos
   (ej. **geo**: tu próximo evento + su lugar).

## Cómo procesa un evento de la extracción

Cuando el orquestador procesa un mensaje, llama `calendar.persist(...)` con los eventos que el LLM
extrajo. **Importante (igual que finanzas):** por esta vía, `persist` solo **guarda el evento crudo y
lo marca** como posible duplicado. **Confirmar** los duplicados, **consolidar**, pulir el texto y
**escribir de vuelta** pasa **después**, en un ciclo por lotes.

```mermaid
flowchart TD
  start["La extracción entrega los eventos<br/>que encontró en un mensaje"]
  start --> filter{"¿hay eventos?"}
  filter -->|no| z["nada que hacer"]
  filter -->|sí| insert["Guarda cada uno CRUDO (tal como llegó)"]
  insert --> dd{"DEDUP rápido: ¿este evento ya estaba?<br/>(se solapan en el tiempo + título/lugar parecido)"}
  dd -->|"posible duplicado"| pair["marca el par como 'candidato' → la IA decide después"]
  dd -->|"sin parecido"| uniq["queda como único"]
  pair --> save["se guarda junto con el mensaje (cursor, atómico)"]
  uniq --> save
  save -. "MÁS TARDE — ciclo por lotes, no en el momento" .-> cycle["La IA confirma los duplicados →<br/>CONSOLIDA en un evento real →<br/>la IA pule el texto → ESCRIBE DE VUELTA al proveedor"]

  classDef later fill:#e5e7eb,stroke:#6b7280,color:#374151;
  class cycle later;
```

En palabras:
1. **Guarda crudo** cada evento (tal como llegó).
2. **Dedup rápido y conservador**: si se solapa en el tiempo con otro **y** el título o el lugar se
   parecen → marca el par como **candidato** (no decide todavía). Si no, queda **único**.
3. **Guarda y cierra** — todo junto con el mensaje, en una sola transacción (si algo falla, se repite).
4. **Lo demás es después** — el ciclo por lotes: la IA **confirma** los duplicados, la consolidación
   funde los grupos en el evento real, la IA **pule el texto**, y el **write-back** lo manda a tu
   calendario externo.

## Las vías de entrada (y la salida)

| Vía | En simple |
|---|---|
| **Mensajes (extracción)** | Automática, vía LLM. Solo guarda crudo + marca duplicados. Consolida **después**. |
| **Tu calendario externo (pull)** | Trae eventos de Google/Outlook directo a la tabla cruda, sin IA. Los que pusiste a mano allá entran con **prioridad alta**. |
| **Otros módulos (contribute)** | Un módulo que dependa de calendario puede **proponer** eventos. Implementado, hoy sin consumidor. |
| **Write-back (SALIDA)** | No es entrada: empuja la **vista consolidada** de regreso al proveedor (lo único que sale del sistema), con anti-bucle. Cada cuenta tiene su **política**: qué estados salen (default solo `confirmado`) y desde cuándo (default: lo que todavía no terminó; una serie sin fin siempre sale). Lo ya escrito que deja de cumplirla se borra del proveedor. `memex-calendar-sync set-push-policy`. |

## Recurrencia (modelo de Google Calendar)

Un evento recurrente es **una sola fila**, el **maestro**, con `recurrence` = líneas RFC 5545
(`RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925`, `EXDATE;VALUE=DATE:20260916`), lo mismo
que Google expone en `event.recurrence`. Las ocurrencias **no se materializan**: se expanden al
leer, dentro de la ventana consultada (`list`, `events_in_range` del dominio, conflictos, la API).
Por eso una serie **sin fin** («todos los jueves») es una regla y no mil filas, y `--until` dejó de
ser obligatorio.

- **Excepción** = una ocurrencia modificada: fila propia con `recurring_event_id` = la serie del
  maestro y `original_start_on` = la ocurrencia que reemplaza (el `originalStartTime` de Google). La
  expansión la pone en lugar de la ocurrencia regular. `calendario update <id> --instance
  YYYY-MM-DD …` la crea.
- **Cancelar una ocurrencia** = `EXDATE` en la regla del maestro (`calendario rm <id> --instance
  YYYY-MM-DD`); la serie sigue. `rm <id>` sin `--instance` borra la serie entera.
- **Extracción**: el extractor devuelve `recurrence` estructurada (`freq`, `byday`, `until`,
  `count`, `interval`) cuando el mensaje describe una repetición («clases de lunes a viernes del
  2 al 25»); `starts_on` es la primera ocurrencia. Regla inválida → evento único, no se descarta.
- **Dedup**: un evento único se compara contra el maestro **proyectado al día** del evento (si la
  serie tiene ocurrencia ese día); dos maestros son par solo con la misma regla y hora. En la
  consolidación la serie manda las fechas: si un correo con «la primera clase» se funde con el
  programa, el consolidado conserva la regla.
- **Conflictos**: los maestros entran expandidos en un horizonte (7 días atrás, 90 adelante); el
  conflicto guarda `occurs_on`, el día concreto del conflicto.
- **Google**: el pull pide `singleEvents=false` (llegan maestros con `recurrence` y excepciones con
  `recurringEventId` + `originalStartTime`); el maestro del proveedor usa su propio id como serie.
  El write-back empuja la regla, no N instancias.

Las series viejas «por instancias» (N filas con `recurring_event_id`) siguen siendo eventos
sueltos válidos; la data de dev es desechable y no se migra.

## Precisiones (lo no obvio)

- **Una propuesta no es un evento.** «Les propongo que hagamos Cripto Tertulia el 26 y el 10,
  ¿quién se apunta para confirmar fechas?» (chat de grupo, 2026-09-26) no fija nada: si nadie
  confirma, no hay nada. El extractor lo salió registrando como `anuncio`; el dueño no quiere
  «anuncios falsos», sobre todo de chats. Dos capas: el prompt dice que una idea, propuesta o
  encuesta de fechas no genera evento ni como anuncio (hay evento cuando el hilo lo deja como
  hecho o la persona dice que va), y una guarda determinista (`calendar/propuestas.py`,
  `drop_chat_proposals`) descarta antes de insertar todo evento que salga solo de chats, con
  estado `anuncio` o `provisional`, cuyo texto suena a propuesta («les propongo», «qué tal si»,
  «quién se apunta», «para confirmar fechas», «podríamos», «les parece»); una marca de plan fijado
  («confirmado», «queda para el 26») gana sobre la propuesta. Lo `confirmado` (la persona va) y
  los correos no pasan por la guarda.
- **Los anuncios de verdad no son tu calendario por default.** `/calendario` abre en «Mis
  eventos» (confirmados + provisionales), recuerda la elección por navegador
  (`lib/calendar-filter.ts`, `memex.calendar.filter`) y los anuncios (convocatorias reales,
  hackathones) aparecen solo con el chip «Anuncios» o «Todo». Tampoco se empujan a Google
  (`push_commitments = ('confirmado',)`). Se vuelven plan cuando vos decís que vas (tu mensaje,
  o pago/inscripción): la consolidación toma el nivel más alto de sus miembros.

- **Una cosa del mundo, un evento** (`docs/principios-generales.md`): un programa o convocatoria
  con varias fechas es UN evento con su rango y las fechas internas en la descripción. El estado
  (`commitment`) se juzga por la situación de la **propia** persona: lo que otro miembro de un
  grupo cuenta de sí mismo («estuve», «me inscribí») no la compromete.
- **La extracción solo deja el evento crudo + lo marca.** Confirmar duplicados, consolidar, pulir y
  escribir de vuelta pasa **después**, en el ciclo por lotes (job `calendar`, cada 30 min, **arranca
  apagado**).
- **Tres fuentes, una sola tabla.** Mensajes, tu calendario externo y otros módulos llenan la misma
  tabla cruda y pasan por el mismo dedup.
- **Consolidar ≠ pulir con IA.** Consolidar (elegir el evento **ganador**) es determinista; la IA solo
  **mejora el texto** (título/lugar/descripción). **La fecha y la hora siempre vienen del ganador**,
  nunca de la IA.
- **Las fechas se guardan SIN zona horaria, a propósito** — porque la IA no resuelve confiablemente la
  zona de "el 14 a las 3PM"; inventar un instante exacto sería falsa precisión.
- **Conflictos → revisión, no fusión.** Dos eventos importantes que chocan de horario se **marcan**
  para que decidas vos; el sistema nunca los une por su cuenta.
- **Todavía no se conecta con otros dominios en el grafo.** Proyecta el nodo "evento", pero aún no
  teje la arista "mismo evento" con finanzas/bienestar (le falta el `event_id` de correlación).

---

## Apéndice técnico

Para quien mantiene el código (`src/memex/modules/calendar/`). Es un `InterestModule` (consume
`EMAIL`+`CHAT`). Mecanismo de unicidad propio (`identity_fields=()`): los crudos coexisten y el vértice
lo materializa la consolidación (ADR-018). Mismo patrón de dos fases que finanzas.

### Equivalencias con el diagrama

| En el diagrama | En el código |
|---|---|
| Guardar crudo (extracción) | `persist`→`dedup` FASE 1 (`module.py`); `mod_calendar_events` `origin='extracción'` |
| Traer tu calendario (pull) | `run_pull` (`sync.py`); `origin='provider'`; idempotente por `etag`/`sync_token`, full-resync ante 410 |
| Eventos de otros módulos | `provide_domain` → `CalendarDomainReader.contribute` (`domain.py`, `origin='module'`) |
| Dedup | FASE 1 `mark_duplicates` (`dedup.py`, determinista, puro) → FASE 2 `run_dedup_phase2` (`dedup_llm.py`, LLM) |
| Consolidar (ganador) | `run_consolidation`: union-find + `pick_winner` (`consolidate.py`); `mod_calendar_consolidated` + `mod_calendar_event_links` (N:1) |
| Pulir texto con IA | `run_merge` (`merge_llm.py`) — estable por firma SHA256; solo texto, fecha/hora del ganador |
| Prioridad / conflictos | `pick_winner` (manual=100, `protected`) + `find_conflicts` (`conflicts.py`) → `mod_calendar_conflicts` (pending review) |
| Write-back (salida) | `run_push` (`sync.py`); echo-suppression por firma + marcador `memex_consolidated_id`; `mod_calendar_writeback` |
| Ciclo por lotes | `run_calendar_cycle` (`scheduler/jobs.py`, job `calendar` PT30M, **arranca apagado**): pull → dedup F2 → consolidación → merge → push |
| Vértice del grafo | `relations/vertices.py` (`mod_calendar_consolidated`, `kind='evento'`, `WHERE NOT deleted`) |
| Cursor | lo escribe el **orquestador** en la misma tx que `persist` |

### Qué guarda (tablas)

Migraciones **0010** (base + decisión fecha/hora NAIVE) y **0011** (sync/consolidación/write-back).

- **`mod_calendar_events`** — eventos **crudos** de las 3 fuentes (`origin`), sin fusión. `starts_on`
  (DATE, ancla obligatoria), `start_time`/`end_time` (TIME, NULL=todo-el-día), `priority_rank`
  (manual=100), `protected`, `provider_event_id` (idempotencia del sync), `processing_outcome`.
- **`mod_calendar_dedup_candidates`** — pares "quizás iguales" (`a<b`, `status`
  candidate/confirmed/rejected, `decided_by` phase1/llm).
- **`mod_calendar_consolidated`** — el **evento real** (el vértice); `winner_event_id`,
  `merge_signature`, `deleted` (tombstone). **El front lee de aquí.**
- **`mod_calendar_event_links`** — qué crudos pertenecen a cada consolidado (N:1).
- **`mod_calendar_conflicts`** — conflictos de horario de alta importancia, para revisión humana.
- **`mod_calendar_provider_accounts`** · **`mod_calendar_writeback`** — cuentas (cursor `sync_token`,
  `write_back`, política `push_commitments`/`push_past_days`) y estado del write-back (etag, firma
  —incluye la recurrencia—, echo-suppression). El token OAuth sale del **vault** de la cuenta del
  dashboard ligada (`account_id`, 0100: el mismo «Google conectado» de /cuenta) o, legacy, de un
  archivo apuntado por env var; nunca en claro en la DB (ADR-001). Las horas salen en la zona local del usuario (`MEMEX_CALENDAR_TIME_ZONE`, default
  `memex.core.localtime`); un `UNTIL` solo-fecha se convierte a fecha-hora UTC al escribir (RFC 5545).
- **`mod_calendar_sync_runs`** · **`mod_calendar_event_changes`** — observabilidad (read-only).

### Archivos clave

| Archivo | Rol |
|---|---|
| `module.py` | `CalendarModule`: contrato, `persist`→`dedup` FASE 1 (insert crudos + marca pares + outcome), superficie de lectura. |
| `schema.py` · `prompt.py` | `CalendarEventItem` (contrato de extracción) y los prompts (extracción + dedup + merge). |
| `dedup.py` · `dedup_llm.py` | FASE 1 determinista (solapamiento + similitud) y FASE 2 LLM. |
| `consolidate.py` · `conflicts.py` | Union-find + `pick_winner` → vértice + links; detección de conflictos de horario. |
| `merge_llm.py` | Enriquecimiento de texto del vértice, estable por firma. |
| `sync.py` | Sync bidireccional: `run_pull` (ingreso) + `run_push` (write-back). |
| `providers/` | `CalendarProvider` (Protocol) + `GoogleCalendarClient` (httpx) + OAuth. |
| `domain.py` · `cli.py` | `CalendarDomainReader` (lectura + `contribute`) y `memex-calendar-sync`. |
| `scheduler/jobs.py` | `run_calendar_cycle` (orquesta el ciclo por lotes). |
| `relations/vertices.py` · `relations/deterministic.py` | Proyección del vértice y co-ocurrencia (no teje `mismo_evento`). |
| `migrations/0010`, `0011` | Esquema base + sync/consolidación/write-back. |
