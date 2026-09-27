# Principios generales de registro

Tres reglas que valen para todo lo que memex registra sobre la persona, sin importar el módulo.
Nacieron de la auditoría de datos del 2026-09-21: lo que fallaba (una charla asignada al proyecto
equivocado, un boletín convertido en cinco tareas, un bootcamp partido en tres eventos, un
remitente partido en dos fichas) no eran errores distintos sino tres criterios que faltaban. El
dueño pidió reglas **generales** —«que casos parecidos pasen y se cubran»— y no excepciones por
caso: por eso viven en un solo lugar y no nombran marcas ni situaciones concretas.

## Los tres principios

1. **Propio o para todos.** Solo es tarea, compromiso, hito o estado de la persona lo que ella
   hizo, decidió, confirmó o le exigen a ella con nombre. Lo dirigido a todos por igual (boletín,
   aviso masivo, llamado a una comunidad, invitación abierta, instrucción condicional «si no…») y
   lo que es solo ánimo, duda o posibilidad («no quiero», «quizás») es información: se registra
   como anuncio, mención o evidencia, nunca como acción, decisión ni obligación suya.
2. **Una cosa del mundo, un registro.** Se registra la unidad que uno seguiría —un programa, una
   convocatoria, una charla, una persona, una compra—, no cada fecha, frase, copia o nombre que la
   menciona. Varias fechas o pasos de una misma cosa son detalle de ese registro.
3. **La señal pertenece al hilo.** Un mensaje se interpreta por la conversación en la que ocurre:
   a quién responde, de qué se venía hablando, quién habla. Nombrar otra cosa de paso no cambia de
   qué trata ni a quién le pasa. Lo que otra persona cuenta de sí misma es de ella.

## Dónde viven

El texto está una sola vez en `src/memex/modules/principios.py` (`PRINCIPIOS_GENERALES`) y entra
tal cual en los prompts de sistema que registran cosas de la persona. Cada uno agrega su lectura:

| Módulo | Prompt | Lectura propia |
|---|---|---|
| tareas | `tareas/prompt.py` | Una tarea nace de una exigencia (entrega o plazo) o de un compromiso propio. Los llamados a la acción de envíos masivos, las instrucciones condicionales y lo que la persona dice que no quiere hacer no son tareas. En un chat de programa, la instrucción del organizador es tarea cuando exige algo; el recordatorio opcional no. |
| calendario | `calendar/prompt.py` y el juez `calendar/commitment.py` | Un programa con varias fechas es un evento con su rango y las fechas internas en la descripción. El estado (`commitment`) se juzga por la situación de la propia persona: lo que otro remitente cuenta de sí mismo no la compromete. |
| identidades | `identidades/prompt.py` | Vínculo es relación directa con el dueño; quien aparece por hacer su trabajo dentro de algo que otro le presta o le anuncia (ponente, quien atiende, la organización que un boletín cita) es mención. El remitente es una sola identidad: su nombre visible no se parte. |
| asuntos | `asuntos/assign.py` | Ánimo no es estado: el estado registra hechos y resultados. El asunto de un mensaje de chat es el de la conversación en la que ocurre; nombrar una entidad de otro asunto no lo cambia. |

`tests/test_principios.py` verifica que los cinco prompts incluyen el bloque y que ninguna regla
nombra marcas ni casos.

## Lo determinista que acompaña a las reglas

- **Nombre visible del remitente de chat** (`identidades/normalize.split_display_name`): «Nombre |
  Org», «Nombre - Org», «Nombre (Org)» y los adornos (emojis, tipografías decorativas) se separan
  al crear la ficha del remitente. La ficha lleva el nombre limpio; el visible queda como alias y la
  organización como pista en `metadata.org_hint`. Un nombre que es pura decoración se conserva tal
  cual. `memex-identidades hygiene` aplica lo mismo a las fichas que ya existían con el nombre
  visible intacto (nunca a una renombrada a mano).
- **Una ficha necesita un vínculo** (`identidades/hygiene.drop_mention_only_identities`): cada
  mención guarda el vínculo que declaró el extractor (`metadata.link`). Una ficha de extracción
  cuyas menciones son todas «solo nombrada» (nadie le escribió, le cobró, la atendió ni la
  organizó) vuelve a ser avistamiento: la ficha se elimina y sus menciones quedan sin identidad.
  Así la regla nueva alcanza a las fichas que nacieron con la regla vieja al reprocesar sus
  mensajes. Una ficha sin ninguna mención ni identificador (huérfana) también se elimina.
- **El asignador ve el hilo** (`asuntos/adhesion.reply_quotes`): un mensaje de chat que responde a
  otro lleva «↩ en respuesta a quién: «texto»», busque el original en el lote o en la tabla. Es la
  misma cita que ya veía el extractor.
- **Reprocesar olvida también los estados** (`asuntos/store.forget`): al volver a leer unos mensajes
  con `--force`, los estados que esos mensajes produjeron se borran y el vigente vuelve a ser el más
  reciente de los que quedan (por fecha del mensaje). Antes el estado que dejó una lectura vieja
  sobrevivía al reproceso.

## Cómo se aplican a datos ya procesados

Las reglas viven en los prompts, así que lo ya extraído no cambia solo: se reprocesa el mensaje
(`memex-reprocess --stage extract,asuntos --force --inbox <id>`) y la limpieza de nombres se corre
con `memex identidad hygiene`. Nunca se corrige la base a mano.
