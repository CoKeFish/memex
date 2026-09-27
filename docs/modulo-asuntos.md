# Módulo asuntos: la ventana del dashboard

Un **asunto** es algo que está en marcha en la vida de la persona (un curso, un trámite, un
proyecto, un evento) y que el sistema infiere de los mensajes: les cuelga evidencia, hitos,
tareas, datos clave y archivos. El diseño del módulo (asignador, curador, adhesión, prioridad,
estados) está en los ADR y en los docstrings del paquete `asuntos`; este documento cubre
la **ventana** del dashboard, que es la superficie de uso diario.

## Dos zonas: uso e inspección

La ventana tiene un propósito de usuario final (se abre a menudo para saber en qué está algo y qué
hay que hacer) y a la vez debe conservar la trazabilidad del dashboard (de qué mensaje salió cada
cosa, con qué método y confianza). Por eso hay dos zonas, separadas por el interruptor
**Inspección** (arriba a la derecha, se recuerda por navegador, `lib/inspect-mode.ts`):

- **Zona de uso** (siempre visible): títulos en frase, sin jerga, acciones como iconos al pasar el
  mouse. Las correcciones de primer nivel siguen ahí (marcar tarea hecha, «no es tarea», quitar un
  dato, quitar una relación) porque son parte de usar el sistema, no de depurarlo.
- **Inspección** (encendido): ids, método y confianza de cada evidencia, historial de estados,
  entidades nombradas, los enlaces a los mensajes con rótulo («chat #123») en vez de solo el icono,
  y el panel «Cómo se armó este asunto» al pie (pestañas Evidencia / Historial de estado /
  Entidades / Técnico). «Quitar» evidencia vive ahí.

## `/asuntos/:id`, de arriba hacia abajo

1. **Cabecera**: tipo, estado, prioridad (solo si es alta), nombre, descripción. Debajo, el
   **estado vigente** como lede (la frase que responde «¿en qué está?») con un lápiz para
   actualizarlo en el lugar; el menú «⋯» tiene renombrar/retipar, fusionar en otro asunto
   (eligiéndolo **por nombre**, `AsuntoPicker`), cerrar/reabrir con nota opcional, **silenciar** y
   borrar (solo candidatos). Una línea gris cierra con desde cuándo, la última señal y cuántos
   mensajes; si está silenciado, esa línea lo dice.
2. **Qué sigue** + **A la mano**, lado a lado. «Qué sigue» es la agenda de lo pendiente con fecha
   (`agendaOf`): tareas sueltas por su vencimiento y proyectos por su **entrega** (la mayor fecha
   entre sus pasos vivos, con el avance «3 de 9 pasos»); lo atrasado va primero en rojo. «A la mano»
   son los datos clave del asunto: un enlace se abre, un código (clase, usuario, teléfono) se
   muestra en mono con botón de copiar, el resto es texto; se anotan con «Anotar» y nunca guardan
   contraseñas (`SecretDatoError`).
3. **Tareas** (pendientes; vencidas y cerradas plegadas) con la misma lista que `/tareas`, y **Qué
   pasó**: los hitos del más reciente al más viejo, agrupados por mes, plegados a 10.
4. Al costado: **Archivos** (la evidencia de medio `documentos`, con «Adjuntar»), **Relacionado
   con** (otros asuntos; relacionar eligiendo por nombre) y **Quiénes** (personas y organizaciones
   del elenco; el resto de vértices, solo en inspección).

## Declarar un asunto y pedirle el plan

Todos los asuntos se **infieren** de los mensajes; este es el único que el dueño **declara**: dice
«sacar el pasaporte es un asunto mío» y le pide el plan. El plan contesta qué hacer, en qué orden,
dónde, qué llevar y cuánto cuesta.

**La regla que manda: fuente oficial o no va.** Un trámite mal explicado cuesta un día de fila, así
que el plan solo afirma lo que encontró en la página de quien manda (la entidad, la universidad, el
proveedor). Blogs, agregadores y foros no sirven ni para confirmar. Cada paso lleva su URL, y lo
que no se pudo confirmar sale aparte en `sin_confirmar`, escrito como lo que hay que averiguar.

**Segunda regla: no inventar fechas.** El sistema no puede pedir la cita por el dueño. Un paso
lleva fecha solo si la fuente publica un plazo real; si no, va sin fecha. Un paso sin fecha es
mejor que una fecha inventada que después no se cumple.

Corre con **codex y búsqueda web en vivo**, igual que los curadores, y por el mismo camino
(`run_codex` con `-c web_search_mode=live --output-schema`), no por `build_llm_client`. Tarda
varios minutos.

Lo que produce se materializa con lo que ya existe: un **proyecto en `mod_tareas` con sus pasos**
colgados y el resumen como estado del asunto. Por eso el plan aparece solo en «Qué sigue», en la
agenda del día y en el resumen de Telegram, sin superficie nueva. Correrlo otra vez no duplica: el
dedup de `tareas.add` reconoce los pasos que ya estaban.

```
memex asuntos add  --name "Sacar el pasaporte" --kind tramite
memex asuntos plan --id 42 [--context "ya tengo la cédula nueva"] [--dry-run]
```

**Tercera regla: siempre una recomendación.** Cuando un paso tiene más de una opción (varias
sedes, varios bancos, presencial o en línea), el plan **elige una** por el dueño y dice por qué en
`por_que`; las otras van en una línea. «Acude a cualquiera de estas sedes» no sirve. Para elegir por
cercanía usa los **lugares de referencia** del dueño (casa, universidad, trabajo), que viven en
`geo_places` con `source='ancla'`; sin ninguno, elige igual y dice qué asumió.

```
memex geo places add --name Casa --address "Calle 1 # 2-3, Bogotá"
memex geo places
```

En la web, el menú «⋯» de la portada tiene «Armar el plan…». En Telegram no hizo falta nada nuevo:
el asistente lee los comandos de `memex help` y propone la escritura para que el dueño la confirme.
Por HTTP: `POST /asuntos` (declarar) y `POST /asuntos/{id}/plan`.

## Contar algo: la palabra del dueño

Debajo del estado hay un cuadro «Contar algo». Ahí va lo que no vino en ningún mensaje: «me
dijeron que el viernes en la tarde puedo pasar, o cualquier otro día», «ya pagué», «la cita
me la corrieron al 30». Lo que escribís queda **como hito** en «Qué pasó», con la fecha del
día, y no depende de nada más: se guarda aunque el modelo no esté.

Encima de eso, un intérprete barato (consumer `asuntos_nota`, DeepSeek) lee la nota junto
con los pasos pendientes del asunto y devuelve qué cambia. Se aplica con las riendas cortas:

- **fecha a un paso**: «el viernes voy a la sede» → el paso «Solicitar el pasaporte
  presencialmente» queda con fecha viernes 18. Solo ids de pasos pendientes, solo fechas
  cercanas (30 días atrás, un año adelante).
- **paso hecho**: «ya pagué» → «Pagar el trámite» completada.
- **plazo del asunto**: solo si la nota da un plazo real («tengo hasta el 30»); el día en
  que pensás ir no es un plazo.
- **estado**: solo si la nota cambia en qué punto está el asunto.

La pantalla dice qué entendió y qué cambió, tal cual, y cada nota guarda esa lista
(`cambios`) para auditarla. Si no entendió nada aplicable, la nota queda como hito y ya.

```
memex asuntos nota --id 277 --text "me dijeron que el viernes en la tarde puedo pasar"
```

En Telegram alcanza con decirlo: el asistente propone ese comando y lo corre cuando
confirmás. Lo que pide re-buscar (una cita nueva, otro camino) sigue siendo «Armar el plan…»
con el contexto en su campo.

## Cerrar: qué pasa con las pendientes

Cerrar un asunto **no** toca sus tareas por su cuenta, y el sistema no puede decidirlo solo porque
los dos casos son legítimos y opuestos: el trámite que terminaste bien deja tareas que YA HICISTE, y
el evento que se te pasó deja tareas que YA NO APLICAN. Así que al cerrar **a mano** se pregunta, y
solo si queda alguna pendiente:

| opción | cuándo | qué hace |
|---|---|---|
| dejarlas | por default | siguen pendientes y se ven atrasadas |
| darlas por hechas | el asunto terminó bien | `completada`, con su `done_at` |
| cancelarlas | ya no aplican | `cancelada`, y salen del grafo |

Solo el cierre MANUAL pregunta. El curador y la regla de vencimiento cierran asuntos sin tocar
tareas: un cierre automático que te cancele pendientes sería peor que el problema. Aparte de esto,
`expire_overdue` vence por fecha las tareas de cursos, hackatones y eventos, pero nunca los pasos de
un proyecto.

```
memex asuntos close --id 42 --state "listo" --tareas completar
```

## Lo que ya pasó no queda abierto

Decisión del dueño (2026-09-17): un hackatón de junio seguía «abierto» en septiembre porque el
modelo no le dio fecha y nadie corrió el curador. Las reglas del sistema (`run_system_rules`, sin
LLM) corren ahora al cierre de **cada corrida del asignador**, no solo en el mantenimiento, y
tienen una vía más:

- **Evento o hackatón con fecha**: último hito pasado hace más de 7 días sin señal posterior →
  «terminó el <fecha>». **Con plazo** (`vence`) vencido hace más de 7 días sin señal → «venció».
- **Evento o hackatón sin ninguna fecha** (ni hito fechado ni plazo) y sin señal hace más de
  `inactive_days / 2` (30 días) → «terminó (sin fecha conocida; sin señal desde el <día>)». Un evento
  ocurre en una fecha: si nadie la dio y hace semanas que no se habla de él, ya pasó. Si después
  llega señal, el asignador lo reabre (los cerrados contemporáneos siguen en el catálogo).
- **Cualquier tipo** sin señal en `inactive_days` (60) → `inactivo`; con señal nueva → `abierto`.
- La palabra del usuario (reabrir o cerrar a mano después de la última señal) frena todo.

`memex-asuntos rules` corre solo estas reglas (sin curador, sin LLM); `maintain` sigue siendo
reglas + curador.

## Ánimo no es estado; el hilo manda

Dos reglas del asignador que vienen de los principios generales (`docs/principios-generales.md`):

- **Ánimo no es estado.** Lo que el usuario siente, duda o dice que no quiere («no quiero
  hacerlo», «no sé si…») no es hito ni actualización: el estado registra hechos y resultados (hizo,
  envió, lo aceptaron, se cerró). Un mensaje que solo trae ánimo va al asunto de su hilo sin tocar
  el estado.
- **El hilo manda.** El asunto de un mensaje de chat es el de la conversación en la que ocurre: a
  qué responde y de qué venían hablando los mensajes anteriores del lote. Para eso el asignador ve
  la cita del mensaje al que responde («↩ en respuesta a quién: «texto»», `adhesion.reply_quotes`),
  igual que el extractor. Nombrar de paso una entidad de otro asunto no cambia el asunto del hilo.

Al reprocesar con `--force`, `store.forget` borra también los estados que esos mensajes produjeron y
vuelve vigente el más reciente de los que quedan: una lectura vieja no sobrevive a la nueva regla.

## Una cosa del mundo, un asunto: gemelos al asignar

Qué pasó (2026-09-25): el catálogo tenía «Beca Solsticio 2026 (FIB)», abierto y con la descripción
«beca de FIB para HackSolsticio 2026, Lisboa 25-26 de octubre», y cuando llegó la confirmación del
organizador («You're on the list for HackSolsticio Lisbon 2026») el asignador abrió «HackSolsticio
Lisbon 2026» como asunto nuevo. Nada lo frenaba: `apply_parsed` creaba lo que el modelo pedía sin
mirar el catálogo, y el único chequeo de duplicados era el curador diario (apagado por el dueño),
cuya regla léxica tampoco habría emparejado esos dos nombres. Con dos asuntos, cada extracción de
tareas duplicó en el suyo lo que ya existía en el otro.

Qué hay ahora (`asuntos/gemelos.py`), antes de crear cada `nuevo:N` del lote:

1. **Reglas baratas sobre el catálogo** (`find_twin_candidates`): nombre o alias igual → gemelo
   seguro, sin juez; ≥ 2 entidades en común (el organizador y el evento), ≥ 2 tokens distintivos
   en común (uno si el del catálogo es candidato), o vecino por SIGNIFICADO (`memex.semantic`,
   slug `asuntos`, texto = nombre + descripción, piso propio 0,70, top-3) → sospecha. Ante el
   juez van primero las de entidades en común, después las de tokens, al final las semánticas
   (`tier`): en el caso real la beca compartía tres entidades y puntuaba 0,81, y otro hackathon
   sin nada en común puntuaba 0,68 y salía primero. Un `servicio` (pagos recurrentes) no se
   empareja con otra clase. Los vectores del catálogo se ponen al día en cada lote
   (`refresh_vectors`, salta lo que no cambió), así cubren todas las vías de creación.
2. **Juez atómico** (`asuntos_judge`, codex con fallback DeepSeek; `judge_same_asunto`): ve (A)
   el asunto del catálogo con descripción, entidades, fechas de señal, estado y una muestra de
   mensajes, y (B) el propuesto; decide si son LA MISMA cosa del mundo vista desde otro canal
   (la confirmación del organizador; el evento y la beca o el viaje para ese evento; la misma
   edición por su sigla) o distintas (otra edición, otro evento del mismo organizador). El
   parecido semántico nunca decide solo: sobre los asuntos del dueño, dos hackathons distintos
   dan 0,83 y el par duplicado real 0,81. Sin juez (proveedor sin configurar) solo valen los
   exactos. Cada veredicto queda en `llm_calls` (`purpose = asuntos_judge`, con `asunto_id`,
   `proposed`, `reason`, `same`, `confidence`).
3. **Redirección** (`apply_parsed(..., twins=)`): el ref `nuevo:N` pasa a apuntar al existente;
   su evidencia, hito, estado y relaciones del lote van ahí; el nombre propuesto queda como alias
   y sus entidades se suman (`store.absorb_name`), así la próxima vez ese nombre adhiere sin
   juez. `AssignResult.twins` cuenta cuántos.

El prompt del asignador también lo dice (regla «UNA COSA DEL MUNDO, UN ASUNTO»): la confirmación
o aceptación que manda el organizador de algo del catálogo se adhiere como hito, nunca abre.

## Cambio de nombre: el mismo asunto con otra marca

Un programa que se renombra («el espacio que conocías como StarForge pasó a ser el Programa de
Embajadores de Sideral») sigue siendo el mismo hilo de vida. El asignador tiene la regla explícita:
adherir al asunto existente y devolver el nombre nuevo en `nombre` dentro de `actualizaciones`;
`store.rename` cambia el nombre y deja el anterior como **alias**, que el catálogo muestra, así el
modelo reconoce las dos formas en los lotes siguientes. Un asunto silenciado no se renombra. La
métrica `renamed` sale en el resultado de la corrida y en la metadata de la llamada LLM.

**Lo que ve el asignador de cada mensaje** (`adhesion.clean_body`) se limpia ANTES del recorte a
`body_chars`: fuera el relleno invisible del preheader (tiras de U+200C y parientes), las URLs de
más de 80 caracteres quedan reducidas a su host (son de rastreo: beehiiv, safelinks) y las citas
del hilo se cortan. El orden importa: el 2026-09-15 un boletín traía 200 caracteres invisibles más
un pixel de rastreo de 400 antes del contenido, el recorte de 900 se los comió y el modelo nunca
leyó la frase del cambio de nombre: abrió un duplicado. El relleno además se quita al ingresar todo
correo (`ingest_service`), también el que llega por push del plugin de Outlook; el parser IMAP ya
lo hacía.

**Fusionar** (`merge_asuntos`, menú «⋯» o `memex-asuntos merge`) mueve evidencia, estados, notas,
relaciones, llaves, alias y también los **datos clave** («A la mano»): mismo valor normalizado =
mismo dato, se unen sus mensajes de origen.

## Silenciar: el hilo que no se puede atender

Cerrar un asunto no lo deja quieto. El curador lo reabre en cuanto vuelve una señal, y pasados los
60 días de `closed_window_days` el asunto sale del catálogo del asignador, así que el aviso
siguiente abre un **duplicado** del mismo tema. Los dos fallos son opuestos y los dos son reales: se
midieron con el NAS del dueño, que repite cada tanto que su certificado caducó y que hubo un apagado
anormal, algo que él no puede resolver.

**Silenciar** (mismo nombre y mismo significado que en Gmail) resuelve los dos:

- El asunto queda **cerrado y nadie lo reabre** salvo el dueño. El control está en `set_status`, por
  donde pasan el curador, el mantenimiento, la promoción de candidato y la fusión: un solo lugar.
- **Nunca sale del catálogo** del asignador, sin ventana de días. Justamente porque no revive, tiene
  que seguir visible para que la señal nueva se adhiera a él en vez de abrir un asunto nuevo.
- La **evidencia sigue entrando**: la historia queda completa y consultable.
- Sus **tareas pendientes se cancelan**. Silenciar es «no voy a hacer nada con esto», y un asunto
  que no vuelve no puede dejar tareas venciendo.
- Reabrirlo a mano levanta el silencio (el dueño pidió verlo otra vez). «Dejar de silenciar» lo
  deja cerrado pero vuelve a permitir que reviva solo.

Nada de esto toca el gate: los avisos de ese remitente siguen siendo tan relevantes como antes, se
guardan y se procesan. Lo único que cambia es que el asunto no vuelve a aparecer.

Superficies: `POST /asuntos/{id}/silence` y `/unsilence`, `memex-asuntos silence|unsilence <id>`, y
el menú «⋯» de la portada. La marca vive en `metadata.silenced_at`, igual que `manual_status_at`.

## `/asuntos` (la lista)

Columnas: asunto (con tipo y, si aplica, «prioridad alta»), estado, **qué sigue**, en qué está,
última señal. «Qué sigue» sale de dos campos nuevos de `AsuntoRow` que el API calcula en una sola
consulta para toda la lista (`_tareas_summary`): `tareas_pendientes` (pendientes vivas; los
proyectos son contenedores y no cuentan) y `proxima_fecha` (la menor fecha entre ellas). Con
inspección encendida se suman evidencia (cantidad de mensajes), alias e id. Las herramientas del
módulo (asignador LLM, curador, ajustes) siguen arriba a la derecha.

## Dónde está cada cosa

- `frontend/src/pages/asunto-story.tsx`, `frontend/src/pages/asuntos.tsx`: las dos vistas.
- `frontend/src/lib/asunto-story.ts` (+ test): agenda, forma del dato, cronología por mes, cortes
  de evidencia y elenco, resumen de tareas. Helpers puros.
- `frontend/src/lib/inspect-mode.ts`: el interruptor compartido.
- `frontend/src/components/features/asuntos/asunto-picker.tsx`: elegir un asunto por nombre.
- `src/memex/api/routers/asuntos.py`: `_tareas_summary` (lista y story).
