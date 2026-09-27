"""Registro de prompts: clave estable → dónde vive en el código y quién lo usa.

Cada `PromptSpec.targets` es la lista de atributos que se sobreescriben: `modulo:NOMBRE` para
una constante de módulo, `modulo:Clase.atributo` para un ClassVar. Importa: el target es el
nombre que el CONSUMIDOR lee en tiempo de ejecución, no solo donde se define. `from x import
NOMBRE` copia la referencia: si `orchestrator.py` importó `ROUTING_SYSTEM_PROMPT`, hay que
sobreescribir `memex.modules.orchestrator:ROUTING_SYSTEM_PROMPT`, no solo el de `routing.py`.
Por eso un prompt puede tener varios targets. `default` se captura del código la primera vez
que se pide."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from functools import cache
from typing import Any


@dataclass(frozen=True)
class PromptSpec:
    key: str
    title: str
    group: str
    description: str
    #: Consumidor LLM (clave de `llm_consumer_settings`) que usa este prompt, si aplica.
    consumer: str = ""
    targets: tuple[str, ...] = ()
    #: False = se muestra como referencia pero no se edita (p. ej. los principios: viven
    #: DENTRO de cada prompt; editarlos aparte no cambiaría nada hasta reiniciar).
    editable: bool = True
    tags: tuple[str, ...] = field(default_factory=tuple)


PROMPTS: tuple[PromptSpec, ...] = (
    # --- pipeline ---------------------------------------------------------------------- #
    PromptSpec(
        "pipeline.routing",
        "Enrutador: qué módulos leen cada mensaje",
        "Pipeline",
        "Decide, por lote, qué módulos de extracción reciben cada mensaje según el `interest` de "
        "cada uno. Un módulo que no se elige no ve el mensaje.",
        consumer="orchestrator",
        targets=(
            "memex.modules.routing:ROUTING_SYSTEM_PROMPT",
            "memex.modules.orchestrator:ROUTING_SYSTEM_PROMPT",
        ),
    ),
    PromptSpec(
        "pipeline.grouping",
        "Extracción agrupada (varios módulos en una llamada)",
        "Pipeline",
        "Cabecera de la llamada que extrae para varios módulos a la vez; el prompt de cada "
        "módulo va adentro.",
        consumer="process",
        targets=(
            "memex.modules.grouping:GROUPED_SYSTEM_PROMPT",
            "memex.modules.orchestrator:GROUPED_SYSTEM_PROMPT",
        ),
    ),
    PromptSpec(
        "principios",
        "Principios generales (referencia)",
        "Pipeline",
        "Las tres reglas que todos los extractores llevan dentro. Viven pegadas en cada prompt: "
        "para cambiarlas en un prompt, editá ese prompt.",
        targets=("memex.modules.principios:PRINCIPIOS_GENERALES",),
        editable=False,
    ),
    # --- tareas ------------------------------------------------------------------------ #
    PromptSpec(
        "tareas.extract",
        "Tareas: extractor",
        "Tareas",
        "Qué es una tarea, categorías, plazos, dinámicas, qué no es tarea.",
        consumer="extract_tareas",
        targets=(
            "memex.modules.tareas.prompt:TAREAS_SYSTEM_PROMPT",
            "memex.modules.tareas.extract:TareasModule.extraction_prompt",
        ),
    ),
    PromptSpec(
        "tareas.interest",
        "Tareas: interés (lo que ve el enrutador)",
        "Tareas",
        "Descripción corta con la que el enrutador decide si un mensaje va al módulo de tareas.",
        consumer="orchestrator",
        targets=("memex.modules.tareas.extract:TareasModule.interest",),
    ),
    PromptSpec(
        "tareas.judge_done",
        "Tareas: juez de cobertura (¿ya la hizo?)",
        "Tareas",
        "¿La candidata es la misma tarea que la persona ya completó?",
        consumer="tareas_judge",
        targets=("memex.modules.tareas.judge:JUDGE_SYSTEM_PROMPT",),
    ),
    PromptSpec(
        "tareas.judge_pending",
        "Tareas: juez de gemelas (¿es la misma pendiente?)",
        "Tareas",
        "¿La candidata es la misma tarea pendiente leída en otra fuente, o una distinta?",
        consumer="tareas_judge",
        targets=("memex.modules.tareas.judge:PENDING_JUDGE_SYSTEM_PROMPT",),
    ),
    # --- calendario -------------------------------------------------------------------- #
    PromptSpec(
        "calendar.extract",
        "Calendario: extractor",
        "Calendario",
        "Fechas y eventos; estado (confirmado, provisional, anuncio); propuestas que no son "
        "evento.",
        consumer="process",
        targets=(
            "memex.modules.calendar.prompt:CALENDAR_SYSTEM_PROMPT",
            "memex.modules.calendar.module:CALENDAR_SYSTEM_PROMPT",
            "memex.modules.calendar.module:CalendarModule.extraction_prompt",
        ),
    ),
    PromptSpec(
        "calendar.interest",
        "Calendario: interés (lo que ve el enrutador)",
        "Calendario",
        "Descripción corta con la que el enrutador decide si un mensaje va al calendario.",
        consumer="orchestrator",
        targets=("memex.modules.calendar.module:CalendarModule.interest",),
    ),
    PromptSpec(
        "calendar.commitment",
        "Calendario: juez de compromiso",
        "Calendario",
        "¿El evento es un compromiso firme, probable o solo informativo para la persona?",
        consumer="calendar_dedup",
        targets=("memex.modules.calendar.commitment:JUDGE_SYSTEM_PROMPT",),
    ),
    PromptSpec(
        "calendar.verify",
        "Calendario: verificación de fechas",
        "Calendario",
        "Segunda lectura de fechas raras (CAP_VERIFY).",
        consumer="calendar_date_verify",
        targets=("memex.modules.calendar.verify:VERIFY_SYSTEM_PROMPT",),
    ),
    PromptSpec(
        "calendar.dedup",
        "Calendario: juez de duplicados",
        "Calendario",
        "¿Dos eventos que se solapan son el mismo?",
        consumer="calendar_dedup",
        targets=(
            "memex.modules.calendar.prompt:CALENDAR_DEDUP_SYSTEM_PROMPT",
            "memex.modules.calendar.dedup_llm:CALENDAR_DEDUP_SYSTEM_PROMPT",
        ),
    ),
    PromptSpec(
        "calendar.merge",
        "Calendario: fusión de copias",
        "Calendario",
        "Cómo se combinan dos copias del mismo evento.",
        consumer="calendar_merge",
        targets=(
            "memex.modules.calendar.prompt:CALENDAR_MERGE_SYSTEM_PROMPT",
            "memex.modules.calendar.merge_llm:CALENDAR_MERGE_SYSTEM_PROMPT",
        ),
    ),
    # --- asuntos ----------------------------------------------------------------------- #
    PromptSpec(
        "asuntos.assign",
        "Asuntos: asignador",
        "Asuntos",
        "Con el catálogo vigente, decide a qué asunto va cada mensaje, abre nuevos, hitos, estado.",
        consumer="asuntos_assign",
        targets=("memex.modules.asuntos.assign:SYSTEM_PROMPT",),
    ),
    PromptSpec(
        "asuntos.judge_twin",
        "Asuntos: juez de gemelos",
        "Asuntos",
        "¿El asunto que el asignador quiere abrir ya existe en el catálogo con otro nombre?",
        consumer="asuntos_judge",
        targets=("memex.modules.asuntos.gemelos:JUDGE_SYSTEM_PROMPT",),
    ),
    PromptSpec(
        "asuntos.nota",
        "Asuntos: intérprete de «contar algo»",
        "Asuntos",
        "Lee la nota del dueño sobre un asunto y devuelve qué cambia.",
        consumer="asuntos_nota",
        targets=("memex.modules.asuntos.notas:NOTA_SYSTEM_PROMPT",),
    ),
    # --- identidades ------------------------------------------------------------------- #
    PromptSpec(
        "identidades.extract",
        "Identidades: extractor",
        "Identidades",
        "Personas y organizaciones que aparecen en los mensajes y cómo se vinculan con la persona.",
        consumer="process",
        targets=(
            "memex.modules.identidades.prompt:IDENTIDADES_SYSTEM_PROMPT",
            "memex.modules.identidades.module:IDENTIDADES_SYSTEM_PROMPT",
            "memex.modules.identidades.module:IdentidadesModule.extraction_prompt",
        ),
    ),
    PromptSpec(
        "identidades.interest",
        "Identidades: interés (lo que ve el enrutador)",
        "Identidades",
        "Descripción corta para el enrutador.",
        consumer="orchestrator",
        targets=("memex.modules.identidades.module:IdentidadesModule.interest",),
    ),
    PromptSpec(
        "identidades.dedup",
        "Identidades: juez de duplicados",
        "Identidades",
        "¿Dos fichas son la misma persona u organización?",
        consumer="identidades_dedup",
        targets=(
            "memex.modules.identidades.prompt:IDENTIDADES_DEDUP_SYSTEM_PROMPT",
            "memex.modules.identidades.dedup_llm:IDENTIDADES_DEDUP_SYSTEM_PROMPT",
        ),
    ),
    PromptSpec(
        "identidades.hierarchy",
        "Identidades: jerarquía",
        "Identidades",
        "Organización ↔ subunidades ↔ personas.",
        consumer="identidades_hierarchy",
        targets=(
            "memex.modules.identidades.prompt:IDENTIDADES_HIERARCHY_SYSTEM_PROMPT",
            "memex.modules.identidades.hierarchy:IDENTIDADES_HIERARCHY_SYSTEM_PROMPT",
        ),
    ),
    PromptSpec(
        "identidades.classify",
        "Identidades: clasificador (persona, organización, producto…)",
        "Identidades",
        "Qué clase de cosa es cada ficha.",
        consumer="identidades_classify",
        targets=(
            "memex.modules.identidades.prompt:IDENTIDADES_CLASSIFY_SYSTEM_PROMPT",
            "memex.modules.identidades.classify:IDENTIDADES_CLASSIFY_SYSTEM_PROMPT",
        ),
    ),
    PromptSpec(
        "identidades.resolve",
        "Identidades: resolvedor contextual",
        "Identidades",
        "Quién es el remitente y a qué ficha corresponde, con el contexto del mensaje.",
        consumer="identidades_resolve",
        targets=(
            "memex.modules.identidades.prompt:IDENTIDADES_RESOLVE_SYSTEM_PROMPT",
            "memex.modules.identidades.resolve_llm:IDENTIDADES_RESOLVE_SYSTEM_PROMPT",
        ),
    ),
    PromptSpec(
        "identidades.verify",
        "Identidades: verificación",
        "Identidades",
        "Segunda lectura de fichas raras (CAP_VERIFY).",
        targets=("memex.modules.identidades.verify_llm:VERIFY_SYSTEM_PROMPT",),
    ),
    PromptSpec(
        "identidades.domain_owner",
        "Identidades: dueño de un dominio de correo",
        "Identidades",
        "A qué organización pertenece un dominio.",
        targets=("memex.modules.identidades.domain_attribution:DOMAIN_OWNER_SYSTEM_PROMPT",),
    ),
    # --- finanzas ---------------------------------------------------------------------- #
    PromptSpec(
        "finance.extract",
        "Finanzas: extractor",
        "Finanzas",
        "Movimientos, montos, contrapartes y rubros a partir de correos y chats.",
        consumer="process",
        targets=(
            "memex.modules.finance.prompt:FINANCE_SYSTEM_PROMPT",
            "memex.modules.finance.module:FINANCE_SYSTEM_PROMPT",
            "memex.modules.finance.module:FinanceModule.extraction_prompt",
        ),
    ),
    PromptSpec(
        "finance.interest",
        "Finanzas: interés (lo que ve el enrutador)",
        "Finanzas",
        "Descripción corta para el enrutador.",
        consumer="orchestrator",
        targets=("memex.modules.finance.module:FinanceModule.interest",),
    ),
    PromptSpec(
        "finance.dedup",
        "Finanzas: juez de duplicados",
        "Finanzas",
        "¿Dos movimientos son el mismo?",
        consumer="finance_dedup",
        targets=(
            "memex.modules.finance.prompt:FINANCE_DEDUP_SYSTEM_PROMPT",
            "memex.modules.finance.dedup_llm:FINANCE_DEDUP_SYSTEM_PROMPT",
        ),
    ),
    PromptSpec(
        "finance.rubros",
        "Finanzas: reclasificación de rubros",
        "Finanzas",
        "«Otros» → el rubro que corresponde, por contraparte.",
        consumer="finance_categorize",
        targets=("memex.modules.finance.rubros:RECATEGORIZE_SYSTEM_PROMPT",),
    ),
    # --- hackathones ------------------------------------------------------------------- #
    PromptSpec(
        "hackathones.extract",
        "Hackatones: extractor",
        "Hackatones",
        "Convocatorias y hackatones con fecha.",
        consumer="process",
        targets=(
            "memex.modules.hackathones.prompt:HACKATHON_SYSTEM_PROMPT",
            "memex.modules.hackathones.module:HACKATHON_SYSTEM_PROMPT",
            "memex.modules.hackathones.module:HackathonModule.extraction_prompt",
        ),
    ),
    PromptSpec(
        "hackathones.interest",
        "Hackatones: interés (lo que ve el enrutador)",
        "Hackatones",
        "Descripción corta para el enrutador.",
        consumer="orchestrator",
        targets=("memex.modules.hackathones.module:HackathonModule.interest",),
    ),
    # --- relevancia -------------------------------------------------------------------- #
    PromptSpec(
        "relevance.gate",
        "Relevancia: compuerta",
        "Relevancia",
        "Decide si un correo es relevante para la persona (filtro previo a todo lo demás).",
        consumer="default",
        targets=(
            "memex.relevance.prompts:GATE_SYSTEM_PROMPT",
            "memex.relevance.judge:GATE_SYSTEM_PROMPT",
        ),
    ),
    PromptSpec(
        "relevance.block_rules",
        "Relevancia: propuesta de reglas de bloqueo",
        "Relevancia",
        "Sugiere reglas de filtro a partir de lo descartado.",
        targets=("memex.relevance.prompts:BLOCK_RULES_SYSTEM_PROMPT",),
    ),
    PromptSpec(
        "relevance.allow_rules",
        "Relevancia: propuesta de reglas de paso",
        "Relevancia",
        "Sugiere reglas de paso a partir de lo aceptado.",
        targets=("memex.relevance.prompts:ALLOW_RULES_SYSTEM_PROMPT",),
    ),
    PromptSpec(
        "relevance.interest_mining",
        "Relevancia: minería de intereses",
        "Relevancia",
        "Descubre intereses de la persona a partir de lo que lee.",
        targets=("memex.relevance.interest_mining:INTEREST_SYSTEM_PROMPT",),
    ),
    # --- grafo / relaciones ------------------------------------------------------------ #
    PromptSpec(
        "relations.propose",
        "Grafo: proponer relaciones",
        "Grafo",
        "Relaciones entre entidades a partir de un mensaje.",
        targets=(
            "memex.relations.prompt:GRAPH_PROPOSE_SYSTEM_PROMPT",
            "memex.relations.per_message:GRAPH_PROPOSE_SYSTEM_PROMPT",
        ),
    ),
    PromptSpec(
        "relations.confirm",
        "Grafo: confirmar relaciones",
        "Grafo",
        "Confirma o descarta relaciones propuestas.",
        consumer="relations_confirm",
        targets=(
            "memex.relations.prompt:GRAPH_CONFIRM_SYSTEM_PROMPT",
            "memex.relations.per_message:GRAPH_CONFIRM_SYSTEM_PROMPT",
        ),
    ),
    PromptSpec(
        "relations.clusters",
        "Grafo: partición en cúmulos",
        "Grafo",
        "Agrupa el grafo en cúmulos con sentido.",
        consumer="relations_clusters",
        targets=(
            "memex.relations.prompt:GRAPH_CLUSTER_PARTITION_SYSTEM_PROMPT",
            "memex.relations.clusters_llm:GRAPH_CLUSTER_PARTITION_SYSTEM_PROMPT",
        ),
    ),
    PromptSpec(
        "relations.summary_individual",
        "Resumen individual de un mensaje",
        "Grafo",
        "Resumen de un mensaje relevante (nivel individual).",
        consumer="summarizer",
        targets=("memex.relations.summary:INDIVIDUAL_SUMMARY_PROMPT",),
    ),
    PromptSpec(
        "relations.summary_batch",
        "Resumen por lote",
        "Grafo",
        "Resumen de un lote de mensajes (nivel batch).",
        consumer="summarizer",
        targets=("memex.relations.summary:BATCH_SUMMARY_PROMPT",),
    ),
    # --- OCR --------------------------------------------------------------------------- #
    PromptSpec(
        "ocr.system",
        "OCR: lectura de imágenes",
        "OCR",
        "Qué se le pide al modelo de visión al leer una imagen.",
        targets=(
            "memex.ocr.prompt:OCR_SYSTEM_PROMPT",
            "memex.ocr.openai_vision:OCR_SYSTEM_PROMPT",
        ),
    ),
)

_BY_KEY: dict[str, PromptSpec] = {p.key: p for p in PROMPTS}
assert len(_BY_KEY) == len(PROMPTS), "claves de prompt repetidas"


def spec(key: str) -> PromptSpec:
    try:
        return _BY_KEY[key]
    except KeyError as e:
        raise KeyError(f"prompt desconocido: {key!r}") from e


def _split_target(target: str) -> tuple[str, list[str]]:
    module_path, _, attr = target.partition(":")
    if not module_path or not attr:
        raise ValueError(f"target inválido: {target!r}")
    return module_path, attr.split(".")


def read_target(target: str) -> str:
    """El texto vigente en ese atributo (lo que el consumidor leería ahora mismo)."""
    module_path, parts = _split_target(target)
    obj: Any = importlib.import_module(module_path)
    for p in parts:
        obj = getattr(obj, p)
    if not isinstance(obj, str):
        raise TypeError(f"{target} no es un str")
    return obj


def write_target(target: str, value: str) -> None:
    module_path, parts = _split_target(target)
    obj: Any = importlib.import_module(module_path)
    for p in parts[:-1]:
        obj = getattr(obj, p)
    setattr(obj, parts[-1], value)


@cache
def default_text(key: str) -> str:
    """El texto del CÓDIGO. Se captura la primera vez que se pide (antes de que ningún override
    lo pise: `overrides.apply_overrides` llama a esto para cada clave antes de escribir)."""
    s = spec(key)
    return read_target(s.targets[0])


def capture_defaults() -> None:
    """Fija los defaults de todas las claves (llamar antes de aplicar cualquier override)."""
    for p in PROMPTS:
        default_text(p.key)
