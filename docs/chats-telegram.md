# Chats de Telegram: qué entra y cómo se procesa

La fuente `telegram` usa la cuenta del usuario (sesión Telethon) y trae SOLO los chats de su lista
blanca (`sources.config.allowed_chats`, por `chat_id`): grupos, supergrupos (con o sin temas) y
canales de difusión. Los mensajes privados nunca entran (el parser los rechaza). Los canales
entran hoy como medio `chat`, igual que los grupos.

## Naturaleza de cada chat (`mode`)

Un chat no es un correo: el mensaje no va dirigido al usuario, es un miembro hablando en un grupo.
Por eso cada entrada de la allowlist declara su naturaleza (decisión del dueño, 2026-09-11):

| `mode` | Qué es | Asuntos | Calendario | Identidades |
|---|---|---|---|---|
| `anuncios` (default) | comunidad; solo interesan los anuncios del organizador | no crea asuntos; solo señal si involucra al usuario | anuncios con fecha | solo el usuario y quien le responde ganan ficha |
| `programa` | el usuario participa un tiempo (cohorte, viaje, curso) | anuncios del organizador → estado del asunto del programa, en tercera persona; la charla de los demás → null | idem | además los recurrentes (≥ 5 mensajes en el chat) |
| `intimo` | grupo cercano (equipo, amigos, familia) | reglas normales | reglas normales | todos ganan ficha |

Finanzas no extrae de chats `anuncios` ni `programa` (lo que se dice de plata es de terceros o
anuncios); solo de grupos `intimo`.

**El grupo nunca es el asunto** (decisión del dueño, 2026-09-17), en los tres modos: el chat es el
lugar donde se habla, y los asuntos son las iniciativas concretas que ahí se planean o se viven
(cada hackathon o convocatoria a la que se presentan, cada proyecto, cada postulación, cada viaje).
Un mismo grupo alimenta varios asuntos a lo largo del tiempo; la coordinación sin iniciativa
concreta va a null. Motivo: un grupo cercano de un equipo que hace hackathons se había vuelto un
único asunto con el nombre del grupo y 366 mensajes, tapando el hackathon, el proyecto y la
postulación que sí eran asuntos.

**Cada pasada drena el chat** (2026-09-20). Antes traía UNA tanda de `batch_size` (100) por chat y
por pasada horaria: un grupo activo quedaba meses atrás y una dinámica llegaba vencida. Ahora pide
tandas hasta que una venga corta o hasta `drain_max_per_run` mensajes vistos (2000; 0 = sin tope),
con el piso entre tandas en el último mensaje VISTO, así una tanda entera de temas no permitidos no
se vuelve a pedir. El cursor persistido sigue siendo el último mensaje conservado.

**Lo que ve el LLM de un chat** (`orchestrator._build_messages`): una RÁFAGA (mensajes seguidos
del mismo remitente, mismo tema, menos de 2 minutos entre uno y otro, hasta 8) va como UN item con
`ids` = todos sus mensajes, porque quien anuncia una dinámica la escribe en cuatro mensajes («por
70 puntos», el acertijo, las reglas, «solo el primero») y separados el extractor veía fragmentos. Y
un mensaje que responde a otro del mismo lote lleva «↩ en respuesta a X: …» al frente: sin eso el
extractor no sabía a quién le hablaban («confírmame nombre y país» era para otra persona).

Se fija en `/cuenta` → Telegram → selector de chats (un select por chat), por API
(`PATCH /sources/{id}` con `allowed_chats[].mode`) o por CLI
(`memex-telegram set-chats --source-id N --chat-ids=… --mode programa`, aplica a todos los
dados). `sources.config.chat_mode_default` cubre chats que no figuren en la lista. Cambiar el modo
no exige re-ingerir: los módulos lo leen al procesar (`memex.modules.chat_context`).

## Perspectiva del usuario

- El parser marca `sender.is_self` (Telethon `message.out`): mensajes escritos por la cuenta del
  usuario. El asignador de asuntos los ve como «LO ESCRIBIÓ EL USUARIO».
- Prompt de asuntos (sección «CHATS DE GRUPO»): lo que otro miembro cuenta de sí mismo (avance,
  ascenso, entregable, pregunta, felicitación) no es hito ni estado del usuario; nunca se redacta
  un estado en primera persona a partir de lo que dijo otro. Sí es señal: mensaje propio, mensaje
  que le habla al usuario, anuncio del organizador que aplica a todos los participantes.
- El resolvedor contextual de identidades (una llamada LLM por mensaje, diseñado para correo) ya
  no corre para chats ni notas: el remitente de chat es determinista y una nota no tiene emisor.

## Primera corrida

`since_days` (default 180): con cursor en 0 el cliente pide desde «hoy − since_days», no desde el
mensaje 1; después manda el cursor (`min_id`). `batch_size` (100) sigue siendo el tope por chat y
por corrida. Sin esto la primera corrida traía el pasado remoto (2021) de cada chat.

## Prueba de referencia

`experiments/chats-eval-2026-09-11.md`: 539 mensajes de 5 chats procesados ANTES de estas reglas
(asuntos atribuían al usuario acciones de terceros; 182 personas creadas por remitente; calendario
de anuncios correcto). Las reglas de arriba salen de esa prueba.
