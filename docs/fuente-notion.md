# Fuente Notion (notas propias)

Los apuntes del usuario en Notion entran a memex como una fuente más: tipo `notion`, categoría
`notes` (`SourceKind.NOTES`). Solo las páginas raíz que el usuario configura y sus subpáginas; una
página es un registro en su versión vigente (editarla lo reemplaza y vuelve a extracción).

## Conectar

1. En Notion: **Settings → Connections → Develop or manage integrations → New integration**
   (interna, en tu workspace, permisos de lectura). Copiar el *Internal Integration Secret*.
2. Compartir SOLO las páginas raíz que quieras: en cada una, menú `…` → **Connections** →
   agregar la integración. Las subpáginas heredan el acceso.
3. En memex, `/cuenta` → agregar cuenta proveedor **Notion (notas)** y pegar el secreto como
   credencial `integration_token` (vault cifrado; nunca en `.env`).
4. Crear la fuente (una por cuenta): `POST /sources` con
   `{"name": "notion-apuntes", "type": "notion", "config": {"roots": ["<id o URL de cada raíz>"]}}`
   y vincularla a la cuenta (`account_id`). Config opcional: `max_depth` (6), `max_pages_per_run`
   (100), `max_blocks_per_page` (2000), `token_env` (default `MEMEX_NOTION_TOKEN`). `roots: []` =
   todo lo compartido con la integración.
5. Traer: `POST /sources/{id}/fetch` (solo modo `incremental`) desde `/ingesta`, o agendar con
   `fetch_schedule` para el daemon de ingesta. Salud: `POST /accounts/{id}/health-check` (401/403 →
   `reauth_required`: volver a pegar el token).

## Cómo funciona (`src/memex/ingestors/notion/`)

| Archivo | Rol |
|---|---|
| `config.py` | `NotionConfig.from_source_config`: token por env var, `roots` normalizados (UUID con guiones, acepta URLs), límites. |
| `client.py` | httpx sync contra `api.notion.com/v1` (`Notion-Version: 2022-06-28`): ~3 req/s, reintento en 429/5xx, paginación. |
| `parser.py` | Bloques → texto markdown llano (encabezados, listas, `[x]` tareas, citas, código, tablas, adjuntos como `[imagen: …]`, subpáginas como `📄 título`) y `NotePayload`. |
| `source.py` | `NotionSource`: búsqueda por edición hasta la marca de agua del cursor, filtro por raíces subiendo por los padres (páginas, bases, bloques; con caché), lectura recursiva de bloques, salida ascendente por edición con tope por corrida. |

**Cursor** (`NotesCursor`): `pages[page_id].last_edited_time` + `watermark`. Solo una página más
nueva que lo recordado vuelve a salir. `advance_checkpoint` sube la marca solo hasta lo flusheado.

**Payload** (`NotePayload`): `provider`, `page_id`, `title`, `body_text`, `url`, `path` (títulos
de los ancestros desde la raíz), `root_id`, `parent_id`, `created_time`, `last_edited_time`,
`archived`, `body_truncated`, `child_page_ids`. `external_id = notion:<page_id>`.

## Ediciones

`insert_record(on_conflict="replace")` (solo para fuentes `notes`, decidido en
`api/ingest_service.py` por la categoría del tipo): mismo `external_id` con payload distinto →
UPDATE de payload y `occurred_at`, resultado `updated`; el borde borra sus `module_extractions`
(vuelve a la cola de extracción), resetea `processed_at` y asuntos olvida la decisión de ese
mensaje (`asuntos.store.forget`). Payload idéntico → `duplicate` sin tocar nada. No se guarda el
historial de versiones (decisión 2026-09-10: evita evidencia duplicada en asuntos y grafo).

## Aguas abajo

- El gate de relevancia es solo de correo: las notas (como los chats) entran directo.
- Asuntos: el alcance ya no exige veredicto para lo que no es correo (`assign._relevant`, la
  cola `unseen_relevant_inbox_ids` y `memex-asuntos assign --since`). Eso también abre asuntos a
  los chats.
- Módulos que consumen `notes`: calendario e identidades. Finanzas no (los movimientos viven en
  correos). El render del «input al LLM» pone `Título: … (ruta)` como encabezado.
- Ventana por medio para `notes`: 10 mensajes (editable en `/procesamiento`).

## Frontend

Tipo `notion` y medio `notes` en selectores, iconos (`StickyNote`), grupo «Notion (notas)» en el
panel de ingesta, proveedor en `/cuenta` (credencial `integration_token`), tab «Notas» en la
documentación de filtros, y resumen de fila por `page_id` (título + cuerpo; «remitente» = ruta).

## Límites conocidos

Bases de datos: sus páginas entran si cuelgan de una raíz (la base es un ancestro); las
propiedades de la base (columnas) no se vuelcan al texto, solo el contenido de la página. Páginas
en la papelera no se traen ni se borran de memex. Sin CLI propio (`memex-notion`): la fuente se
opera por API/dashboard como el resto.
