"""Gate de relevancia por intereses personales — portero del pipeline para correos.

Corre ANTES de todo procesamiento LLM (resumen + ruteo/extracción) y SOLO sobre correos
(SourceKind.EMAIL). Conoce los intereses personales del usuario (`personal_interests`) y emite
un veredicto por mensaje (`relevance_verdicts`): `relevant` sigue el pipeline normal,
`not_relevant` queda en /datos pero ningún LLM posterior lo toca, `insufficient` cae a la cola
de revisión manual. NO borra nada.

Capas:

- `settings`/`interests`: configuración y catálogo de intereses (CRUD; apagado por default).
- `rules`: reglas DETERMINISTAS del gate (propuestas por minería LLM o manuales), validadas con
  dry run contra el histórico y auto-activadas solo si no atrapan ningún correo relevante.
- `verdicts`: el cursor del gate + la cláusula que filtra los worksets de summarize/extract.
- `gate`/`mining` (LLM): el worker del veredicto (Opus, modos per_window/per_message) y la
  segunda pasada que propone reglas a partir de los no-relevantes.

Rompe a propósito la doctrina «advisory nunca acciona» del sistema de calidad: este módulo SÍ
acciona (bloquea procesamiento, crea reglas), siempre auditado y reversible. La automejora vive
acá como módulo PREVIO al pipeline — excepción acotada a la exclusión de ADR-015.
"""

from memex.relevance.interests import (
    create_interest,
    delete_interest,
    list_interests,
    update_interest,
)
from memex.relevance.rules import (
    DryRunReport,
    apply_active_rules,
    create_rule,
    dry_run_rule,
    list_rules,
    match_rule,
    set_rule_status,
)
from memex.relevance.settings import GateSettings, get_settings, upsert_settings
from memex.relevance.verdicts import (
    EMAIL_TYPES,
    VerdictItem,
    clear_verdicts,
    insert_verdicts,
    list_review_queue,
    load_gate_workset,
    resolve_insufficient,
    workset_gate_clause,
)

__all__ = [
    "EMAIL_TYPES",
    "DryRunReport",
    "GateSettings",
    "VerdictItem",
    "apply_active_rules",
    "clear_verdicts",
    "create_interest",
    "create_rule",
    "delete_interest",
    "dry_run_rule",
    "get_settings",
    "insert_verdicts",
    "list_interests",
    "list_review_queue",
    "list_rules",
    "load_gate_workset",
    "match_rule",
    "resolve_insufficient",
    "set_rule_status",
    "update_interest",
    "upsert_settings",
    "workset_gate_clause",
]
