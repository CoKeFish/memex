"""Filtro pre-ingest determinístico — drop puro de records que matcheen reglas.

Aplica `filter_rules` a la cola de `SourceRecord` ANTES de que toquen `inbox`.
Resultado: si una regla con `action='ignore'` matchea un record, ese record
NO se persiste — solo se emite un structlog event con counter agregado por
`rule_id`. Sin metadata del record (no sender, no asunto, no contenido) por
diseño: drop puro.

**Mini-DSL para `scope`** (JSONB del filter_rule):

  `scope` es un dict cuyas keys son paths dentro del `payload` del record.
  Cada value es un objeto con UN operador:

  - `{"equals": X}`    — igualdad estricta.
  - `{"in": [X, Y]}`   — membresía en lista.
  - `{"regex": "..."}` — match con regex (re.search).
  - `{"prefix": "..."}` — prefix match en string.

  Si `scope` tiene varias keys, todas deben matchear (AND).

  Ejemplos:

    `{"from": {"equals": "spam@x.com"}}`
    `{"sender_name": {"regex": "^bot:"}}`
    `{"chat_id": {"in": [-100123, -100456]}}`
    `{"subject": {"prefix": "[NEWSLETTER]"}}`

  Las keys del scope son paths DOT-NOTATION dentro del payload — `"from"`
  matchea `payload["from"]`. Paths anidados (ej. `"from.email"`) son
  futuros; v1 soporta solo top-level.

**Aplicación por orden de prioridad**: las reglas con prioridad mayor evalúan
primero. La primera que matchea decide el destino del record. Reglas sin
match → record pasa (default keep).

**Acciones**:

  - `ignore` — drop puro (no inbox, sí counter).
  - `keep`   — record pasa (explícito).
  - `archive` — bucket previsto pero NO implementado; trata como `keep`
    por ahora. Documentado en el plan como TODO de fase posterior.

Para uso desde streaming (Fase 3), también se exporta
`DeterministicFilterMiddleware` — versión chain-compatible con
`memex.core.middleware.build_handler`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from memex.core.middleware import IngestContext, IngestMiddleware, Next
from memex.core.source import SourceRecord
from memex.logging import get_logger

_log = get_logger("memex.core.filters")

FilterAction = Literal["keep", "ignore", "archive"]

RulesLoader = Callable[[int, str | None, int | None], "list[FilterRule]"]
"""(user_id, source_type, source_id) -> reglas activas, ordenadas por prioridad."""


class FilterRule(BaseModel):
    """Mirror Pydantic de una fila de `filter_rules`."""

    id: int
    user_id: int
    source_type: str | None
    source_id: int | None
    scope: dict[str, Any]
    action: FilterAction
    priority: int
    enabled: bool

    model_config = ConfigDict(frozen=True, extra="forbid")


def load_active_rules(
    conn: Connection,
    *,
    user_id: int,
    source_type: str | None,
    source_id: int | None,
) -> list[FilterRule]:
    """Carga reglas activas aplicables al record en orden de prioridad descendente.

    Una regla aplica si:
      - `user_id` matchea (siempre requerido).
      - `source_type` es NULL (global por user) o matchea el del record.
      - `source_id` es NULL (regla por tipo) o matchea el del record.

    El index `filter_rules_lookup` cubre este query.
    """
    rows = (
        conn.execute(
            text(
                """
            SELECT id, user_id, source_type, source_id, scope, action, priority, enabled
            FROM filter_rules
            WHERE enabled
              AND user_id = :uid
              AND (source_type IS NULL OR source_type = :stype)
              AND (source_id IS NULL OR source_id = :sid)
            ORDER BY priority DESC, id ASC
            """
            ),
            {"uid": user_id, "stype": source_type, "sid": source_id},
        )
        .mappings()
        .all()
    )
    return [FilterRule.model_validate(dict(r)) for r in rows]


def evaluate(rule: FilterRule, payload: dict[str, Any]) -> bool:
    """True si todas las keys del scope matchean el payload (AND).

    Scope vacío matchea cualquier cosa — útil para reglas "drop everything
    from this source" o "keep all" (combinada con action explícita).
    """
    for key, op_spec in rule.scope.items():
        if not isinstance(op_spec, dict) or not op_spec:
            return False
        value = payload.get(key)
        if not _match_one(value, op_spec):
            return False
    return True


def _match_one(value: Any, op_spec: dict[str, Any]) -> bool:
    """Evalúa un operador del mini-DSL contra un value del payload."""
    if "equals" in op_spec:
        return bool(value == op_spec["equals"])
    if "in" in op_spec:
        choices = op_spec["in"]
        if not isinstance(choices, list):
            return False
        return value in choices
    if "regex" in op_spec:
        pattern = op_spec["regex"]
        if not isinstance(value, str) or not isinstance(pattern, str):
            return False
        try:
            return re.search(pattern, value) is not None
        except re.error:
            return False
    if "prefix" in op_spec:
        prefix = op_spec["prefix"]
        if not isinstance(value, str) or not isinstance(prefix, str):
            return False
        return value.startswith(prefix)
    # Operador desconocido → no matchea (defensivo).
    return False


def decide(rules: Iterable[FilterRule], payload: dict[str, Any]) -> FilterRule | None:
    """Primera regla que matchea (en orden de prioridad). None si ninguna."""
    for rule in rules:
        if evaluate(rule, payload):
            return rule
    return None


def apply(
    records: Iterable[SourceRecord],
    rules: list[FilterRule],
    *,
    source_id: int | None = None,
    source_type: str | None = None,
) -> tuple[list[SourceRecord], dict[int, int]]:
    """Filtra records contra las reglas; retorna (records que pasan, drops por rule_id).

    `source_id` y `source_type` se aceptan solo para emitir el structlog event
    con contexto — el filtrado de reglas aplicables ya se hizo en
    `load_active_rules`.
    """
    kept: list[SourceRecord] = []
    drops: dict[int, int] = {}
    for record in records:
        rule = decide(rules, record.payload)
        if rule is None or rule.action != "ignore":
            kept.append(record)
            continue
        drops[rule.id] = drops.get(rule.id, 0) + 1
    if drops:
        for rule_id, count in drops.items():
            _log.info(
                "pre_ingest.drop",
                rule_id=rule_id,
                source_id=source_id,
                source_type=source_type,
                count=count,
            )
    return kept, drops


class DeterministicFilterMiddleware(IngestMiddleware):
    """Middleware streaming-compatible: dropea o pasa según filter_rules.

    Construido por el StreamingRunner con un cargador de reglas inyectado
    (típicamente request-scoped / per-event para que cambios en la DB
    surtan efecto sin restart). Para Fase 1 polling, la versión `apply()`
    funcional es lo que usan los routers HTTP.

    Llamar `next(record)` si la regla matcheada es `keep`/`archive` o no
    matchea ninguna. Hacer drop puro (no `next`) si matchea `ignore`.
    """

    def __init__(self, load_rules: RulesLoader) -> None:
        self._load = load_rules

    async def __call__(
        self,
        record: SourceRecord,
        ctx: IngestContext,
        next: Next,
    ) -> None:
        rules = self._load(ctx.user_id, ctx.source_type, ctx.source_id)
        rule = decide(rules, record.payload)
        if rule is not None and rule.action == "ignore":
            _log.info(
                "pre_ingest.drop",
                rule_id=rule.id,
                source_id=ctx.source_id,
                source_type=ctx.source_type,
                count=1,
            )
            return
        await next(record)
