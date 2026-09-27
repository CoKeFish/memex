# Prompts: verlos y editarlos desde el dashboard

Pedido del dueño (2026-09-26): «quiero una ventana donde incluso los pueda modificar, pero con
botón de confirmación de edición». Hasta hoy cada prompt vivía solo en el código (un
`*_SYSTEM_PROMPT` por módulo, los jueces, el enrutador) y cambiarlo era un commit y un despliegue.

## Dónde están

| Capa | Qué hace |
|---|---|
| `src/memex/prompts/registry.py` | El registro: cada `PromptSpec` tiene clave estable (`tareas.extract`, `calendar.commitment`…), título, grupo, descripción, consumidor LLM y los **targets**: los atributos exactos que cada consumidor lee en tiempo de ejecución (`modulo:NOMBRE` o `modulo:Clase.atributo`). |
| `src/memex/prompts/overrides.py` | La tabla `prompt_overrides` (texto vigente por clave) y `prompt_override_versions` (historial). `set_override` / `reset_override` / `revert_to_version` escriben y **aplican en el acto**; `refresh_if_stale` reaplica en cada proceso. |
| `src/memex/api/routers/prompts.py` | `GET /prompts`, `GET /prompts/{key}`, `PUT` (guardar), `DELETE` (restablecer al código), `POST /{key}/revert/{version_id}`. |
| `frontend/src/pages/prompts.tsx` | La ventana `/prompts` (menú Proceso): lista por grupo, editor, **diálogo de confirmación** con el resumen del cambio y una nota, restablecer, versiones. |
| Migración 0115 | Las dos tablas. |

## Cómo funciona un cambio

1. El dueño edita el texto y pulsa «Guardar cambios…». El diálogo muestra cuántas líneas entran
   y salen (`lib/prompts-diff.ts`), las primeras de cada lado, y pide una nota. Nada se aplica
   hasta «Confirmar y aplicar».
2. La API valida (no vacío, tope de tamaño; avisa, sin bloquear, si el texto perdió los
   principios generales o el formato JSON de respuesta), guarda la fila vigente, deja una
   versión y **escribe el texto sobre cada target** en su propio proceso.
3. Los demás procesos (scheduler, asistente, notificaciones) lo ven por `refresh_if_stale()`,
   que `build_llm_client` llama antes de cualquier uso del LLM: como mucho una consulta chica
   cada 30 s (`max(updated_at)`); si cambió, reaplica todos los overrides y restaura el código
   donde ya no hay override. Sin reiniciar nada.
4. «Restablecer al código» borra la fila vigente (el historial queda) y vuelve el texto del
   código. Guardar un texto idéntico al del código equivale a restablecer.

## Por qué targets y no una función `get_prompt()`

Los consumidores leen constantes de módulo o ClassVars (`ChatMessage("system",
JUDGE_SYSTEM_PROMPT)`, `unit.modules[0].extraction_prompt`). `from x import NOMBRE` copia la
referencia: sobreescribir `routing.ROUTING_SYSTEM_PROMPT` no cambia lo que `orchestrator.py`
importó. Por eso una clave puede tener varios targets (el de origen y el de cada importador), y
el test `tests/prompts/test_registry.py` exige que todos lean el mismo texto. Un prompt nuevo en
el código = una entrada nueva en el registro con sus targets; si se renombra un atributo, ese
test lo dice.

## Lo que no cambia

- El **código sigue siendo el default** y lo protegen los tests de siempre (sin marcas ni casos
  del dueño, con los principios). El override es una capa encima, siempre reversible.
- Los principios generales se muestran como referencia (`principios`), no se editan aparte:
  viven pegados dentro de cada prompt; para cambiarlos en un prompt, se edita ese prompt.
- `interest` de cada módulo (lo que ve el enrutador) también es editable: decide qué mensajes
  llegan a cada extractor.
