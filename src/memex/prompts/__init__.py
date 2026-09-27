"""Prompts editables desde el dashboard (2026-09-26).

Los prompts viven en el código (un `*_SYSTEM_PROMPT` por módulo, los jueces, el enrutador) y
cambiarlos era un commit y un despliegue. El dueño quiere verlos y editarlos en una ventana, con
confirmación antes de aplicar. Diseño:

- `registry`: la lista de prompts conocidos (`PromptSpec`): clave estable, título, grupo,
  descripción y los ATRIBUTOS exactos que cada consumidor lee en tiempo de ejecución
  (`modulo:NOMBRE` o `modulo:Clase.atributo`). El texto del código es el DEFAULT y se captura
  al cargar el registro; nunca se pierde.
- `overrides`: la tabla `prompt_overrides` (una fila por clave con el texto vigente) y su
  historial `prompt_override_versions`. `apply_overrides` escribe cada override sobre sus
  atributos (y restaura el default donde ya no hay override); `refresh_if_stale` lo hace cada
  `REFRESH_TTL_S` en cada proceso (API, scheduler, asistente) desde `build_llm_client`, así un
  cambio en la UI llega a todos los consumidores sin reiniciar.

El código sigue siendo la fuente de verdad del DEFAULT; el override es una capa encima que se
puede quitar («Restablecer al código»). Los tests que protegen los prompts (sin marcas, con los
principios) siguen corriendo sobre el código."""

from memex.prompts.registry import PROMPTS, PromptSpec, default_text, spec

__all__ = ["PROMPTS", "PromptSpec", "default_text", "spec"]
