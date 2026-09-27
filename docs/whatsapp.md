# WhatsApp: dispositivo vinculado

memex se vincula a tu WhatsApp como un dispositivo más, igual que WhatsApp Web. Recibe los mensajes
en vivo y, al vincular, el tramo de historial que WhatsApp decida mandar. **Solo entran los chats que
vos incluís**; el resto queda en una bandeja de espera y se descarta a los 30 días.

> **Riesgo asumido.** Un cliente no oficial viola los términos de WhatsApp. Meta ha mostrado avisos
> («tu cuenta podría estar usando herramientas no autorizadas») y ha baneado cuentas, incluso con
> uso bajo y solo de lectura. memex nunca envía mensajes, presencia ni recibos de lectura desde la
> sesión vinculada, pero el riesgo no desaparece. Decisión del dueño, 2026-09-12.

## Cómo se usa

1. En `/cuenta`, «Agregar cuenta» → proveedor **WhatsApp** → alias. No pide credenciales.
2. En la tarjeta, **Vincular / Chats** → escribí tu número con indicativo y sin `+`
   (`57300XXXXXXX`) → **Pedir código**.
3. En el teléfono: WhatsApp → Dispositivos vinculados → Vincular un dispositivo → **Vincular con
   número de teléfono** → escribí el código. Dura pocos minutos; si vence, pedí otro.
4. Al quedar vinculado, la tarjeta pasa al **catálogo de chats**: cada chat y grupo que la sesión fue
   viendo, con cuántos mensajes y hasta qué fecha. Marcá los que entran, elegí su **naturaleza**
   (anuncios · programa · íntimo, la misma de Telegram) y, si querés, un **desde** (solo mensajes a
   partir de esa fecha). **Guardar selección** vuelca la bandeja de los chats recién incluidos al
   inbox.
5. **Desvincular** cierra la sesión en WhatsApp y borra la base del dispositivo.

## Qué pasa por dentro

| Pieza | Dónde |
|---|---|
| Cliente vivo (neonize → whatsmeow), uno por cuenta | `memex/whatsapp/runtime.py`, en el proceso del API (lifespan), como el streaming de Telegram |
| Sesión del dispositivo (sqlite, sensible) | `MEMEX_WA_SESSION_PATH/account_<id>.db` → en compose `/secrets/whatsapp` |
| Vínculo (estado, teléfono, JID, error) | tabla `whatsapp_links` |
| Catálogo de chats | tabla `whatsapp_chats` |
| Bandeja de chats no incluidos (crudo, 30 días) | tabla `whatsapp_buffer` |
| Selección de chats | `sources.config.allowed_chats` de la fuente `whatsapp` de la cuenta (formato Telegram; `chat_id` = JID) |
| Mensaje → inbox | `ingestors/whatsapp/parser.py` → `WhatsAppPayload` (mismos nombres que Telegram) → `ingest_one_record` con sus medios |
| API | `/accounts/{id}/whatsapp/{status,pair,chats,unlink}` |
| «Validar» en /cuenta | `memex/whatsapp/health.py` vía `sources.resolve_device_health` (fuente push: estado del vínculo + conexión viva, sin credenciales) |

Un mensaje de un chat incluido entra al inbox con `external_id = whatsapp:<jid>:<id>`; sus medios
(fotos, documentos, audios) se bajan y van a MinIO como cualquier adjunto, y el OCR los lee antes de
extraer (en un grupo de curso la tarea y su fecha suelen venir en la foto del tablero, no en el
texto). La bandeja guarda el mensaje crudo **con las llaves del archivo, no los bytes**: al incluir
el chat entra el texto de inmediato y los archivos se bajan en segundo plano con el cliente vivo
(pocos a la vez) y se adjuntan a cada mensaje (`whatsapp.media.backfill` en el log, con cuántos
entraron y cuántos fallaron). Un archivo que WhatsApp ya expiró en sus servidores queda sin bajar.
Un mensaje que ya estaba en el inbox sin archivo recibe el archivo si vuelve a llegar (una
re-vinculación trae el historial otra vez) y vuelve a la cola de extracción. La clave
`whatsapp:<chat>:<id>` es identidad del mensaje para el usuario (dedup ENTRE fuentes, como el
Message-ID de un correo): si la cuenta se borra y se crea otra, el historial no entra dos
veces y los archivos se suman a la fila que ya existía. Dos fuentes que ya quedaron
duplicadas se unen con `memex-sources merge --from <vieja> --into <nueva> --apply`. El «desde» filtra tanto
la bandeja como lo que llega en vivo.

El OCR necesita la llave del proveedor de visión en el contenedor (`OCR_API_KEY`, o la canónica
`OPENAI_API_KEY` que el compose pasa desde Doppler); sin ella las fotos quedan `pending` y el
mensaje espera (el work-set no extrae un mensaje con medios sin leer).

## Descripción del grupo

La descripción (tema) de un grupo suele traer lo que no está en los mensajes: la carpeta con las
diapositivas, reglas, horarios. Llega con el historial (`Conversation.description`) y en vivo con
`get_group_info` (al conectar y al incluir un chat). Se guarda en `whatsapp_chats.description` y,
para un chat incluido, entra al inbox como un mensaje del propio grupo (remitente «descripción del
grupo», `external_id whatsapp:<jid>:descripcion:<sha8>`), una fila por versión del texto: así la
procesan los módulos (enlaces como recursos de tareas, evidencia del asunto).

## Límites

- **Historial**: lo que manda WhatsApp al vincular es decisión suya (`requireFullSync` con tope de
  dos años); whatsmeow no permite pedir historial viejo a demanda. La bandeja de 30 días es el
  margen para elegir chats sin apuro.
- **Estados y canales** (`status@broadcast`, newsletters) se ignoran. Stickers entran como mensaje
  sin archivo. Reacciones, mensajes de protocolo y stubs de sistema no son mensajes.
- **Un solo dispositivo memex por cuenta**: vincular de nuevo borra la sesión anterior.
- **Baneo**: si WhatsApp suspende el dispositivo, la tarjeta lo muestra (`baneado`) y la sesión deja
  de recibir. No hay nada que memex pueda hacer al respecto.
