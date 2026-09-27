"""PRINCIPIOS GENERALES de registro: las reglas de sistema que comparten todos los módulos.

Nacen de la auditoría de datos del 2026-09-21 (ver `docs/principios-generales.md`). El dueño pidió
que fueran GENERALES —«que casos parecidos pasen y se cubran»— y no excepciones por caso: por eso
viven en UN solo lugar y cada prompt de extracción/asignación (tareas, calendario, identidades,
asuntos, juez de estado de calendario) los incluye tal cual, y después aplica su lectura propia
(«por el principio 1, en este módulo…»). Sin marcas ni casos del dueño (regla
`feedback-prompts-sin-marcas`): describen categorías.

Los tres:

1. **Propio o para todos.** Solo es tarea, compromiso, hito o estado de la persona lo que ELLA
   hizo, decidió, confirmó o le exigen a ella con nombre. Lo dirigido a todos por igual y lo que es
   solo ánimo o posibilidad es información (anuncio, mención, evidencia).
2. **Una cosa del mundo, un registro.** Se registra la unidad que uno seguiría, no cada fecha,
   frase, copia o nombre que la menciona.
3. **La señal pertenece al hilo.** Un mensaje se interpreta por la conversación en la que ocurre;
   nombrar otra cosa de paso no cambia de qué trata ni a quién le pasa.
"""

from __future__ import annotations

PRINCIPIOS_GENERALES = (
    "PRINCIPIOS GENERALES (valen para todo lo que registres; ante la duda, aplicalos):\n"
    "1. PROPIO O PARA TODOS. Solo es tarea, compromiso, hito o estado de la persona lo que\n"
    "   ELLA hizo, decidió, confirmó o le exigen a ella con nombre. Lo dirigido a todos por\n"
    "   igual (un boletín, un aviso masivo, un llamado a una comunidad, una invitación abierta,\n"
    "   una instrucción condicional «si no…» / «si todavía no…») y lo que es solo ánimo, duda,\n"
    "   ganas o posibilidad («no quiero», «no sé si», «quizás», «me gustaría») es INFORMACIÓN:\n"
    "   se registra como anuncio, mención o evidencia, nunca como acción, decisión ni\n"
    "   obligación suya.\n"
    "2. UNA COSA DEL MUNDO, UN REGISTRO. Registrá la unidad que uno seguiría —un programa, una\n"
    "   convocatoria, una charla, una persona, una compra—, no cada fecha, frase, copia o\n"
    "   nombre que la menciona. Varias fechas o pasos de una misma cosa son detalle de ese\n"
    "   registro, no registros nuevos; varias cosas distintas en un mismo mensaje sí son\n"
    "   varios registros.\n"
    "3. LA SEÑAL PERTENECE AL HILO. Un mensaje se interpreta por la conversación en la que\n"
    "   ocurre: a quién responde, de qué se venía hablando, quién habla. Nombrar otra cosa de\n"
    "   paso no cambia de qué trata ni a quién le pasa. Lo que OTRA persona cuenta de sí misma\n"
    "   («estuve», «me inscribí», «ya lo hice») es de ella, no de la persona.\n"
)
