# Módulo tareas — arquitectura

> En el código: **`tareas`** (`src/memex/modules/tareas/`), CLI `memex-tareas`, grupo `memex tareas`
> del agente. Migración `0091_tareas`.

Una **tarea** es una **acción pendiente**: «ejercicios de la página 42 para el jueves» (del curso del
Britano), «cambiar el punto de atención de la EPS» (sin fecha). Tiene título, fecha límite
**opcional**, prioridad, estado (pendiente / completada / cancelada / vencida) y, opcionalmente, el **asunto** al
que pertenece.

Qué NO es:

- **Calendario** modela cosas que **ocurren** en un momento (la clase del Britano). Una tarea no
  ocurre, se hace; a lo sumo vence. Un recordatorio con fecha y hora («el 1 de octubre pagar Nubix»)
  sí es del calendario.
- **Asuntos** son hilos de vida **inferidos de los mensajes**, con estado y evidencia. Una tarea puede
  colgar de un asunto (el curso), pero no necesita uno («cambiar la EPS» es suelta).

Lo maneja el **agente** (Telegram → codex → CLI): vos lo decís en lenguaje natural, él estructura y
llama a memex. memex es **determinista**: no usa IA ni lee tus mensajes; guarda, **deduplica** y
conecta. No hay extracción automática de tareas desde correos o chats: es demasiado selectivo para
hacerlo a ciegas; si algún día se quiere, será por remitentes/canales elegidos, no por defecto.

## Contrato del módulo

| Regla | tareas |
|---|---|
| Nivel | 2 (depende de **asuntos**; sin asuntos las tareas siguen funcionando, solo sueltas) |
| Persiste | `mod_tareas` (título, descripción, `due_on`, prioridad 1-3, estado, `asunto_id`, `done_at`, `metadata`, `event_id`) |
| Entrada | CLI `memex-tareas` / `memex tareas` (agente), determinista, sin LLM |
| Dedup | **clave natural + resolución difusa**, en el write-path (ver abajo) |
| Vértice | `tarea` (`mod_tareas`, `status <> 'cancelada'`), kind `tarea` |
| Aristas | `tarea → asunto` «parte_de» (`producer='tareas'`, confirmed determinista), tejida al crear y al re-ligar; reparación en `weave_all_reales` |
| Nivel grafo (no del módulo) | «mismo_evento» por `event_id` (el agente correlaciona los hechos de un mensaje) |
| Público | `add`, `list`, `show`, `done`, `reopen`, `cancel`, `update`; API `GET /tareas`, `GET /tareas/{id}` |
| Mantenimiento | `rm` (borra la fila) |

## Dedup

Al crear, **antes** de insertar:

1. **Clave natural:** `memex_norm(title)` (sin acentos, minúsculas, espacios colapsados) igual al de
   una tarea **pendiente** del **mismo asunto** (si la nueva trae asunto) o de **cualquier** pendiente
   (si viene suelta). Hay índice por `(user_id, memex_norm(title))`. **Regla temporal**
   (2026-09-26): si el plazo de la existente ya había vencido el día del mensaje nuevo y ese
   mensaje no trae fecha propia, el título igual NO decide: la misma consigna genérica («revisar
   el sitio web y tomar notas») se repite cada sesión con otro material, y la lámina de la sesión
   6 se había fundido en la tarea de la sesión 2 (única sobre-fusión hallada al revisar a mano las
   330 tareas). Ese par va al juez con las fechas a la vista, con la pista explícita de que una
   consigna repetida después de vencida es nueva salvo prórroga o recordatorio. Con fecha propia
   en el mensaje nuevo sigue siendo la misma entrega con el plazo movido (se reabre, ver abajo).
   Referencia: *temporal record linkage* (Li, Dong, Maurino, Srivastava, VLDB 2011): el mismo
   valor en dos momentos lejanos puede ser dos entidades.
2. **Resolución difusa:** títulos casi iguales (`difflib` ≥ 0,85 sobre el texto normalizado) con fecha
   límite **compatible** (igual, o alguna sin fecha). «Pagar matrícula del Britano» ≈ «Pagar la
   matrícula del Britano»; dos tareas parecidas con fechas distintas son dos tareas.

3. **Banda media → juez** (solo en la extracción): si la regla barata no decide pero la candidata
   se parece a una tarea viva del mismo asunto (≥ 2 tokens de contenido y ≥ 40 % del título
   corto, o el MISMO mensaje con ≥ 1 token, o la MISMA fecha límite con ≥ 1 token), el juez
   `tareas_judge` (codex) decide si es la misma obligación leída en otra fuente (chat, nota de
   clase, diapositiva, correo) o una tarea distinta. Tope: 4 candidatas por item.
4. **Los pasos del plan siempre van al juez.** El parecido por tokens quita el nombre del asunto
   («pasaporte») y no conoce sinónimos («reclamar» / «recoger»): «Reclamar el pasaporte
   ordinario» nunca llegaba a compararse con el paso del plan «Recoger y revisar el pasaporte» y
   nacía duplicada (2026-09-24). Ahora una tarea suelta que llega a un asunto se contrasta con
   los pasos vivos de los proyectos del asunto aunque no compartan palabras (`plan_step`), y un
   paso ya completado del plan también cuenta como cobertura posible (`find_completed_cover`);
   en ambos casos decide el juez. Entre los pasos del plan, primero el MÁS parecido por
   significado aunque quede bajo el piso semántico (2026-09-26: al juez llegaba «llenar el
   formulario de la beca» por ser el último completado, y no «inscribirte en X y elegir track»,
   que era el paso que la confirmación cumplía). `memex-tareas recheck --ids …` revisa pendientes ya creadas con
   las dos vías.
5. **Bloqueo semántico** (`docs/semantic.md`): además de las léxicas, las tareas vivas del asunto
   que se parecen por SIGNIFICADO (embeddings locales, coseno, top-3 con piso 0,62) van al juez
   como `semantic`; una completada parecida por significado cubre; dos títulos con números y sin
   ninguno en común no se proponen por significado. Nunca funde solo: el juez decide.
6. **El alcance es la FAMILIA del asunto** (`family_of`: el asunto y los vivos relacionados con él
   por `parte_de` / `requisito_de` / `continua`, un salto, cualquier dirección). Antes cada
   asunto se comparaba solo consigo mismo y la misma obligación nacía en el programa (por el chat
   del grupo) y en la beca que es parte de él (por el correo): «grabar el video de 60 s» llegó a
   existir tres veces en dos asuntos relacionados (2026-09-25). Las candidatas, la cobertura de
   completadas y la búsqueda por título (`find_task_by_title`, la que usa `kind = hecha`) ven la
   familia; las gemelas del hermano llegan marcadas `other_asunto` y detrás de las del propio
   asunto; el nombre de ningún asunto de la familia cuenta como parecido. Complemento del mismo
   día: el asunto de una tarea de chat lo decide el nombre que dio el modelo cuando apunta a un
   asunto de la familia del dominante del chat (`resolve_asunto_for`); el chat solo manda cuando
   el modelo no nombra nada de esa familia.

Un duplicado **no se inserta**: se devuelve la tarea existente con `deduped: true` y `matched_by`.
`--force` lo saltea («no, es otra»). Una tarea **completada o cancelada no bloquea**: repetirla es una
tarea nueva.

**La misma tarea por varias vías enriquece, no duplica** (`enrich`, determinista): la lectura nueva
suma su mensaje a la procedencia y sus enlaces; agrega el detalle nuevo a la descripción con el día
del mensaje (si ya estaba dicho no repite; si contiene a la anterior la reemplaza) y acumula la
evidencia; la prioridad sube si la nueva es mayor; la fecha se toma si no había y se **mueve** si
el mensaje nuevo es posterior al que la fijó (`metadata.due_fixed_day`; queda `due_history`), lo
que **reabre** una vencida que vuelve a pedirse con fecha vigente; un mensaje más viejo no la pisa.
El título se conserva (estabilidad) y la otra redacción queda en `metadata.aliases`. Una tarea
**descartada** por la persona («no es tarea») no se recrea ni se toca.

## Enganche tardío: una nota no dice de qué curso es

Una tarea sacada de un chat ya nace con su asunto (el curso del chat). Una sacada de una **nota**
(Notion) o de un correo suelto no: el asunto se lo pone el asignador de asuntos minutos después,
cuando cuelga ese mensaje del curso (`adopt_orphan_tareas`). Hasta 2026-09-16 ese enganche solo
colgaba la tarea, y así quedaban dos huecos: la tarea «para la próxima clase» sin fecha (la fecha
sale del horario del curso, que no se sabía al extraer) y **duplicados** (el chequeo de gemelas
mira dentro del asunto, y la tarea de la nota estaba suelta cuando llegó la del chat).

Ahora el enganche hace lo que la extracción hubiera hecho de haber sabido el asunto:

- le pone la fecha de la próxima sesión (`date_next_session_for`, mismo horario del curso);
- la marca (`metadata.dedup_pending`) y, al cerrar el lote del asignador, el **re-chequeo**
  (`extract.recheck_duplicates`) la compara con las del asunto con la misma regla barata y el mismo
  juez codex de la extracción; si es la misma, `merge_tareas` la funde en la que ya existía.

```
memex tareas recheck [--dry-run] [--since YYYY-MM-DD | --ids 1,2]   # mantenimiento
memex tareas relink --apply       # también fecha las enganchadas antes que quedaron sin fecha
memex tareas merge --into 278 --absorb 272   # a mano (web, consola, Telegram)
```

Fundir = la absorbida enriquece a la que queda (su redacción como alias, detalle, cita, fecha si
faltaba, prioridad si sube, enlaces, procedencia, proyecto si no tenía) y queda **cancelada** con
`metadata.merged_into`, así el extractor no la vuelve a crear si relee su mensaje. La API es
`POST /tareas/{id}/merge {absorb_id}`.

## Orden de `list`

Vencidas primero, luego por fecha límite ascendente, las **sin fecha al final**; dentro de cada
grupo, prioridad (alta primero) e id. `overdue` viene calculado en la fila (hoy en Bogotá).

## CLI

```
memex tareas add --title "Ejercicios pág. 42" --due 2026-09-11 --asunto Britano --json
memex tareas add --title "Cambiar punto de atención EPS"            # sin fecha
memex tareas list --json            # pendientes
memex tareas list --overdue         # solo vencidas
memex tareas list --due-before 2026-09-15 --asunto Britano
memex tareas done --id 3            # reopen / cancel / update / show
```

Dentro de un evento multi-hecho (`memex start … end`) los `tareas add` se encolan como los demás
hechos y se persisten juntos al cerrar, con el mismo `event_id`.

## Archivos clave

- `src/memex/modules/tareas/module.py` — `add` (dedup), `find_duplicate`, `list_tareas`, `set_status`,
  `update`, `delete`, `resolve_asunto`.
- `src/memex/modules/tareas/cli.py` — `memex-tareas`; `add_from_args` lo reusa el cierre de evento.
- `src/memex/relations/deterministic.py` — `_materialize_tarea_asunto`, `weave_tarea`; brazo `tarea`
  en `_materialize_same_event`.
- `src/memex/relations/vertices.py` / `edges.py` — `NodeSource('tarea')`, `PRODUCER_TAREAS`,
  `RELTYPE_PARTE_DE`, `TAREA_SLUG`.
- `src/memex/api/routers/tareas.py` — lectura.
- `tests/tareas/` — módulo, CLI (+ agente + evento), grafo.

## Dónde verlas

- **/asuntos/:id** → panel «tareas»: pendientes, vencidas y cerradas (plegadas) del asunto, con
  completar / reabrir / cancelar.
- **/tareas**: todas, con filtro por estado y texto, y el asunto de cada una.
- API: `GET /tareas?asunto_id=…&status=all`, `POST /tareas/{id}/status {status}`.
- CLI: `memex-tareas list --asunto <id|texto> --status all`.

## Categorías: de qué clase de cosa es cada pendiente

Pedido del dueño (2026-09-26): los avisos de seguridad de sus cuentas deben quedar como tarea
«por si acaso», y los pendientes deben poder verse por prioridad o por categoría. Columna
`mod_tareas.category` (migración 0113), vocabulario fijo (`tareas.CATEGORIES`):

| categoría | qué es |
|---|---|
| `seguridad` | aviso de seguridad de una cuenta propia: intento de acceso, dispositivo nuevo, cambio de contraseña, restablecer 2FA, actividad sospechosa |
| `entrega` | algo que producir y enviar: trabajo, ejercicio, documento, video, publicación |
| `tramite` | papeles, formularios, citas o confirmaciones con una institución o servicio |
| `pago` | pagar, cobrar, renovar |
| `inscripcion` | registrarse, postularse, anotarse |
| `dinamica` | reto o dinámica de un programa (con puntos: se fuerza aunque el modelo diga otra cosa) |
| `otro` | el resto (no se muestra como chip) |

- **Extractor**: `TareaItem.category`; el prompt define cada categoría. **Seguridad** es la
  excepción explícita a «una instrucción condicional de un aviso no es tarea»: «si no fuiste vos,
  hacé clic» sí es tarea (`Verificar el intento de acceso a X`, prioridad alta), porque la persona
  tiene que verificar que fue ella. Sin fecha, vale `extract.SEGURIDAD_DIAS` (2) desde el mensaje
  (`metadata.due_source = 'seguridad'`): después ya no hay nada que verificar y vence sola.
- **Lista**: `list_tareas` pone las de seguridad primero (un acceso ajeno no espera), después el
  orden de siempre; filtro `category` en el módulo, el API (`GET /tareas?category=`) y el CLI
  (`--categoria` en `add`, `list` y `update`).
- **Dashboard**: chip con la categoría en cada fila (seguridad en rojo) y «Por categoría» como
  tercera forma de agrupar en `/tareas`, en el orden fijo de la tabla, seguridad primero.

## Recordatorios del calendario: lo que hay que HACER también vive ahí

«Pagar arriendo y administración» estaba en Google Calendar (evento recurrente de día completo)
y no aparecía en tareas: el módulo solo extraía de mensajes (2026-09-26). En el calendario
conviven lo que PASA (reunión, clase, cumpleaños) y lo que hay que HACER (pagar, renovar,
enviar); lo segundo es tarea. `tareas/recordatorios.py`, determinista y sin LLM:

- **Qué es un recordatorio**: un evento consolidado de **día completo** (sin hora), **confirmado**,
  cuyo título **empieza con un verbo de acción** en infinitivo (`VERBO_CATEGORIA`: pagar, renovar,
  enviar, llamar…). Un cumpleaños, una reunión con hora o un anuncio no lo son. Se lee por el
  handle de dominio del calendario (`CalendarDomainReader.events_in_range`), que ya expande las
  recurrencias y respeta las excepciones, nunca la tabla cruda.
- **Cuándo nace la tarea**: cada ocurrencia dentro de los próximos `HORIZONTE_DIAS` (7) se
  materializa una sola vez (`metadata.calendar_event_id` + `occurrence`), con `due_on` = el día,
  `origin = 'calendar'`, categoría según el verbo (pagar → `pago`, renovar → `tramite`, enviar
  → `entrega`…). Completarla o cancelarla no la resucita; la del mes siguiente nace a su tiempo.
  No la vence el sistema: un arriendo no «vence», sigue debiéndose (queda atrasada).
- **Dónde corre**: en el ciclo de tareas del scheduler (`run_tareas_cycle`, cada 6 h) y a mano con
  `memex-tareas recordatorios [--dias N] [--apply]`.

## Modelo

La extracción de tareas corre en su propia unidad con el consumer `extract_tareas` (no en la
llamada agrupada con los demás módulos), así su proveedor se elige aparte: `memex-llm settings set
--consumer extract_tareas --provider codex --fallback deepseek` la manda a codex cuando DeepSeek
no rinde en este dominio (decisión del dueño, 2026-09-13).

## Láminas de un curso: fecha por sesión

Un documento no tiene fecha de mensaje (la de subida no dice nada de la clase). Si el asunto tiene
horario (evento recurrente confirmado) y el nombre del archivo trae el número de sesión (`3.pdf`,
`B1-1_10.pdf`, `Clase 7.pdf`; un `(1)` de descarga repetida no cuenta), la lámina k se fecha en la
k-ésima sesión y sus «class preparation» vencen en la sesión k+1 (`metadata.session_day`). Lo
pedido en clases ya pasadas nace con fecha pasada y el cierre automático lo marca vencido. Sin
número o sin horario, la tarea queda sin fecha.

## Datos clave del asunto

Un mensaje que suelta un dato que hace falta tener a mano —el código de clase de la plataforma, el
enlace de acceso, el salón, el número de expediente— entra como `kind = dato` (`title` = qué es,
`value` = el dato tal cual) y va a `mod_asuntos.metadata.datos` del asunto (dedup por valor, con los
mensajes de origen). Nunca un secreto: una contraseña, PIN, OTP o clave temporal se rechaza en todos
los caminos (`SecretDatoError`); el correo que la trae sigue siendo evidencia, pero el dato no se
copia. Se ven y se editan en el panel «datos» de /asuntos/:id; API `POST
/asuntos/{id}/datos` y `/datos/remove`; CLI `memex-asuntos dato --id N --label … --value …`.

## Recursos (enlaces)

A mano: `memex-tareas link --id N --url https://…` (repetible).

Cada item trae `links`: las URLs de los mensajes que hacen falta para la tarea (la plantilla, el
video, el sitio); se guardan en `metadata.links` y el dashboard los muestra como chips con el
dominio. Un mensaje que es SOLO un enlace de un proyecto ya mencionado (la plantilla de Canva) entra
como `kind = recurso`: no crea tarea, suma el link (y el mensaje) al proyecto existente o a la tarea
que el nombre describe; un recurso nunca crea un proyecto. Un proyecto muestra sus links más los de
sus pasos. Dos pasos del mismo proyecto que comparten la mayoría de las palabras (≥ 60 %, ≥ 2
tokens) son el mismo paso.

## Pasos de un proyecto

Un proyecto de curso («el proyecto de Canva») se pide por pasos en mensajes distintos. El extractor
marca cada paso con `parent_hint` (el nombre corto del proyecto, igual para todos sus pasos) y el
sistema los cuelga de una tarea PADRE (`metadata.is_parent`, sin fecha) por `parent_id`: se reusa si
ya existe en el asunto (nombre igual o contenido), se crea si no. Cada fuente nombra el proyecto
a su manera («Proyecto de Instagram en Canva» en la lámina, «el proyecto en curso» en la nota,
«proyecto de posts» en el chat): un nombre GENÉRICO (solo palabras como proyecto, final, curso,
entrega, posts) con un ÚNICO proyecto vivo en el asunto es ese proyecto; y el padre se crea solo
cuando el paso resulta NUEVO (o el existente no tenía padre), así un paso repetido con otro nombre
de proyecto no deja un proyecto vacío. Un nivel. El dashboard muestra el
proyecto con «n de m pasos» y sus pasos debajo; con más de tres pasos empieza plegado y se abre
desde su propia fila (la pastilla «n de m pasos» con flecha, o el título), no desde un link aparte
(el dueño no lo encontraba, 2026-09-17). A mano: `memex-tareas add --parent <id>`, `update
--parent <id>` / `--clear-parent`, `POST /tareas/{id}/parent {parent_id}`.

**Un proyecto nace con dos pasos** (2026-09-17). El modelo pone `parent_hint` también a una tarea
suelta con encabezado («Homework: …», «Tarea de la semana») y nacía un «proyecto» de un paso,
repetido y vacío a la vista. Ahora el padre se crea cuando el nombre lo comparten al menos dos
pasos (en la misma pasada o uno ya registrado suelto con ese `metadata.parent_hint` en el asunto),
o cuando el proyecto ya existe; mientras tanto el paso queda suelto recordando el nombre y se
cuelga solo cuando aparece el segundo.

**Proyectos repetidos.** La extracción corre antes que la asignación de asuntos: una nota sin chat
hace nacer el proyecto suelto, y al colgarse del asunto (`adopt_orphan_tareas`) se compara con los
proyectos que ya tiene (nombre igual o contenido: «Proyecto de Instagram» ⊂ «Proyecto de Instagram
en Canva (My Social Life)») y se funde (`merge_parents`: pasos, procedencia y enlaces pasan al que
sobrevive, el nombre absorbido queda en `metadata.aliases`). Para lo que ya estaba repetido:
`memex-tareas dedup-parents [--asunto N] [--apply] [--flatten-singles]`; sobrevive el que tiene más
pasos, y `--flatten-singles` aplica hacia atrás la regla de los dos pasos (el paso queda suelto, el
padre se borra).

## Dinámicas de un programa

Una dinámica, reto o desafío de un programa (en un chat de modo `programa`) que da puntos SÍ es
una tarea de la persona: el dueño las quiere para sumar puntos (2026-09-20). Lo que fallaba: llegaban
sin fecha aunque el texto la traía («antes del viernes 3:30 pm», «hasta el lunes a la hora de la
llamada»), las de «al primero que responda» quedaban pendientes para siempre, y el título salía vago
porque la dinámica viene en cuatro mensajes seguidos que el extractor veía separados.

- `due_kind`: `mismo_dia` (se gana o se pierde ese día: «al primero», «hoy», «antes de que termine
  la llamada») → `due_on` = el día del mensaje; `una_semana` (consigna sin plazo dicho) → el día
  del mensaje + el **plazo típico del asunto** (`ritmo.plazo_tipico`: la mediana de los plazos que
  sí vinieron dichos en ese asunto; en el programa de embajadores, 3 días: se anuncia el martes y
  cierra el viernes a la hora de la llamada) o 7 si no hay historia; queda en
  `metadata.plazo_inferido_dias`. Con plazo en el texto, `due_on` lo trae deducido desde `ts`. Como
  pertenecen a un asunto `programa`, vencen solas al pasar la fecha.
- `points`: los puntos que da; quedan en `metadata.puntos` y marcan `metadata.dinamica = true`.
- El lote que ve el LLM agrupa la ráfaga del organizador en un solo mensaje con `ids` y muestra
  «↩ en respuesta a X» cuando un mensaje responde a otro (ver `docs/chats-telegram.md`).
- NO es tarea de la persona: lo que el organizador le pide a OTRA persona (una respuesta a alguien
  más), lo dirigido a otro país o grupo, la pregunta o el pedido de otro miembro, y los cierres
  («se la llevan X y Y», «palabra cerrada»).

## Hechas desde tus propios mensajes

En un chat, tu propio mensaje se le muestra al extractor como «yo (la propia persona)». Si ese
mensaje ES la entrega o la confirmación de una tarea pedida antes (la foto de tu autoevaluación
llena, tu post, «ya lo mandé»), el extractor lo marca `kind = hecha` y el sistema completa la
tarea que corresponde con `done_at` = la fecha del mensaje y la evidencia en `metadata.done_*`; si
no existía, la registra ya completada.

**La confirmación del organizador también completa** (2026-09-25). «You're on the list», «recibimos
tu solicitud», «pago recibido», «documento aprobado» dirigidos a la persona son `kind = hecha` con
`title` = el paso cumplido; un aviso masivo o promocional no confirma nada. Para encontrar la viva
que corresponde: primero por título en la familia del asunto (`find_task_by_title`); si no hay, la
más parecida por SIGNIFICADO entre las vivas de la familia (`find_live_semantic`) pasa por el juez
`tareas_judge`; si afirma, se completa esa (`complete_from_evidence(target_id=)`); si no, la
confirmación se registra aparte, ya hecha. Antes la confirmación de una inscripción que ya estaba
como pendiente no la cerraba (y, si el asignador la había mandado a otro asunto, ni la veía).

## Cobertura de lo ya completado

Lo que marcaste hecho cubre lo que se te pidió hasta ese momento: una candidata del mismo asunto
parecida a una tarea completada, pedida en un mensaje no posterior a la fecha de completado, no
vuelve a ser un pendiente. Título igual → se descarta directo; parecido (≥ 2 tokens y ≥ 50 %) →
el juez `tareas_judge` (codex) decide si es la misma consigna repetida o una asignación nueva
(otro video, otra entrega). Un mensaje posterior a la completada siempre es nuevo.

## Correcciones del usuario

- **No es tarea** (dashboard, `POST /tareas/{id}/discard`, `memex-tareas discard --id N`): la
  extracción se equivocó (una exhortación, un anuncio). Queda `cancelada` con
  `metadata.discard_reason`, sale del grafo, **bloquea** que la extracción la vuelva a crear (misma
  procedencia o mismo título) y se registra como feedback `wrong_extraction` sobre el mensaje de
  origen (captura diferida: el prompt se ajusta a mano con esa evidencia, no solo).
- **Completar / reabrir / cancelar** desde el dashboard o la CLI.
- **Re-extraer no pisa decisiones**: el dedup reconoce primero por PROCEDENCIA (mismo mensaje,
  cualquier estado), así una completada o descartada no reaparece como pendiente; `--force`
  borra y rehace solo las extraídas que siguen pendientes o vencidas.

## Vencidas

Una tarea atada a una sesión (`due_kind = proxima_sesion`: «preparación para la próxima clase») o de
un asunto de tipo `programa` (un curso), `hackathon` o `evento` deja de valer cuando pasa su fecha:
la clase o el evento ya ocurrió. En un hackatón o evento, además, una tarea SIN fecha cuyo mensaje
de origen tiene más de 30 días también vence (el evento pasó); no así en cursos ni trámites. El
sistema la cierra solo como **vencida** (distinto de completada: no dice que se hizo; distinto de
cancelada: no fue decisión de la persona). Un **paso de un proyecto** nunca vence por su propia
fecha: sigue pendiente hasta que se hace o hasta que vence el proyecto; en el dashboard los pasos son
una lista de chequeo sin fecha a la vista (el día en que se pidió cada uno va en el tooltip) y el
proyecto lleva la única fecha, la entrega (la mayor de sus pasos). Un **proyecto** de un asunto de
esos tipos vence cuando todos sus pasos tienen fecha y la última pasó hace más que su **gracia**, y
sus pasos pendientes vencen con él. La gracia sale del **ritmo del asunto** (`tareas/ritmo.py`,
deducido de sus datos, sin LLM): un proyecto cuyos pasos llegaron un solo día es una consigna
cerrada (una dinámica) y su gracia es el plazo típico del asunto, entre 1 y 7 días; un proyecto que
recibe pasos en varios días (un curso) tiene de gracia el doble de su cadencia, entre 7 y 30 días;
sin historia del asunto, 30 días. Con un paso sin fecha sigue vivo, y un paso nuevo vigente revive
un proyecto vencido (re-leer un mensaje viejo no). `memex-tareas ritmo --asunto N` muestra el plazo
típico y, por proyecto, días con pasos, cadencia, gracia y cuándo vence. Se puede reabrir con
`reopen`. Lo hace la propia
extracción al persistir, el job `tareas` del scheduler (cada 6 h, sin LLM) y `memex-tareas expire
[--apply]`. Una tarea con fecha explícita de otro tipo de asunto sigue pendiente y se ve vencida (como
en Todoist): quizá todavía se pueda hacer. Una vencida **sí bloquea** el dedup: re-leer el mismo
mensaje no la resucita como pendiente.

## Un participante más: pedidos abiertos y respuestas a otros

En un chat de programa o comunidad la persona es un participante más (2026-09-26). No es tarea
suya un pedido opcional o condicionado al grupo («si pueden compartan fotos», «los que
quieran… reaccionen», «si postulan avísenme»), un pedido a un subgrupo ajeno («los ticos…»), una
pregunta de otro participante, ni un pedido al equipo organizador («team, pásenme la lista»).
El prompt lo dice; los diez pendientes de ese tipo que había nacieron antes de los principios y
al re-extraerlos con las reglas de hoy ocho desaparecieron.

Los dos que sobrevivían eran **respuestas a otra persona** («confírmame nombre, país y
estrategia» respondiendo a alguien que dijo «yo!»). Dos arreglos: la cita «↩ en respuesta a X:
…» se busca en la tabla cuando el original no está en el lote (`orchestrator._fetch_reply_originals`;
antes solo se citaba dentro de la ventana), y una regla **determinista** en `persist`
(`_addressed_to_someone_else`): una tarea cuyas fuentes son solo respuestas al mensaje de un
tercero (ni del usuario ni de quien escribe) se descarta, salvo que el texto hable al grupo
(«todos», «cada uno»). El modelo veía la cita y aun así anotaba la tarea; por eso no depende de él.

## Extracción desde los mensajes

Desde 2026-09-13 `tareas` también es un módulo de extracción (`memex-extract enable tareas`): el LLM
lee cada ventana con el prompt de tareas y lo que la persona tiene que HACER entra por la misma
puerta que una tarea manual (`add`, con su dedup). Cada tarea extraída guarda de qué mensajes salió
y la cita que la justifica; re-extraer un mensaje olvida solo lo extraído, nunca lo manual.

- **Asunto**: si el mensaje ya tiene evidencia de un asunto, la tarea nace colgada de él; si no,
  del asunto DOMINANTE del chat de donde salió (≥ 3 evidencias y ≥ 80 % de las del chat: en un
  grupo de curso lo que pide el docente es del curso aunque ese mensaje no esté decidido); si no,
  del que nombra el extractor (`asunto_hint`) si existe. Si nada aplica, nace suelta y la etapa de
  asuntos la adopta cuando asienta evidencia sobre ese mensaje. `memex-tareas relink [--apply]`
  vuelve a resolver las sueltas con la regla vigente.
- **Fecha «para la próxima sesión»**: cuando el mensaje ata la tarea a la próxima clase/sesión sin
  dar fecha (`due_kind = proxima_sesion`), la fecha es la siguiente ocurrencia, después del día
  del mensaje, de los eventos recurrentes confirmados del asunto (ligados por evidencia, o con el
  mismo título que uno ligado). Sin calendario del curso la tarea queda sin fecha. Una tarea que
  ya existía sin fecha la toma cuando una lectura posterior la trae.
- **Chats de grupo**: solo se extraen tareas en chats de naturaleza `intimo` o `programa` (donde
  la persona participa). En `anuncios` las obligaciones son de otros.
- **Qué no es tarea**: un evento con hora (calendario), un anuncio general, algo que debe hacer
  otra persona nombrada, o algo ya hecho.
- **Principios generales** (`docs/principios-generales.md`): una tarea nace de una EXIGENCIA a la
  persona (entrega o plazo) o de un compromiso propio. Los llamados a la acción de un envío masivo
  (leer algo, anotarse en una lista, dar feedback), las instrucciones condicionales («si no te
  llegó…») y lo que la persona dice que no quiere hacer no son tareas.
