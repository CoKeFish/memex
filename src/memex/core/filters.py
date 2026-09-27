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
  matchea `payload["from"]`; `"from.email"` desciende dicts anidados (ver
  `_resolve`). Si un segmento falta o no es dict, la key no matchea.

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

import asyncio
import json
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


def _resolve(payload: dict[str, Any], path: str) -> Any:
    """Resuelve una key del scope contra el payload: top-level o dot-notation anidada.

    "from" -> payload["from"]; "from.email" -> payload["from"]["email"]. None si algún segmento
    falta o no es dict. Necesario para reglas por remitente (el from del email es {email, name}, no
    un string)."""
    if "." not in path:
        return payload.get(path)
    cur: Any = payload
    for seg in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    return cur


def evaluate(rule: FilterRule, payload: dict[str, Any]) -> bool:
    """True si todas las keys del scope matchean el payload (AND).

    Scope vacío matchea cualquier cosa — útil para reglas "drop everything
    from this source" o "keep all" (combinada con action explícita).
    """
    for key, op_spec in rule.scope.items():
        if not isinstance(op_spec, dict) or not op_spec:
            return False
        value = _resolve(payload, key)
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


# --- CRUD de reglas (compartido por el CLI `memex-filters` y el router HTTP /filters) ------- #


def create_rule(
    conn: Connection,
    *,
    user_id: int,
    source_type: str | None,
    source_id: int | None,
    scope: dict[str, Any],
    action: FilterAction,
    priority: int = 100,
    enabled: bool = True,
) -> int:
    """Inserta una `filter_rule` y devuelve su id. Idempotente: si ya existe una idéntica
    (mismo user/source/scope/action), devuelve su id en vez de crear un duplicado — así bloquear o
    descartar dos veces el mismo remitente no duplica reglas. El CHECK valida `action`."""
    existing = conn.execute(
        text(
            """
            SELECT id FROM filter_rules
            WHERE user_id = :uid
              AND source_type IS NOT DISTINCT FROM :stype
              AND source_id IS NOT DISTINCT FROM :sid
              AND action = :action
              AND scope = CAST(:scope AS JSONB)
            ORDER BY id LIMIT 1
            """
        ),
        {
            "uid": user_id,
            "stype": source_type,
            "sid": source_id,
            "action": action,
            "scope": json.dumps(scope),
        },
    ).scalar()
    if existing is not None:
        return int(existing)
    new_id = conn.execute(
        text(
            """
            INSERT INTO filter_rules
                (user_id, source_type, source_id, scope, action, priority, enabled)
            VALUES (:uid, :stype, :sid, CAST(:scope AS JSONB), :action, :prio, :enabled)
            RETURNING id
            """
        ),
        {
            "uid": user_id,
            "stype": source_type,
            "sid": source_id,
            "scope": json.dumps(scope),
            "action": action,
            "prio": priority,
            "enabled": enabled,
        },
    ).scalar_one()
    return int(new_id)


def list_rules(
    conn: Connection,
    *,
    user_id: int | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    enabled_only: bool = False,
) -> list[FilterRule]:
    """Lista reglas (todas, o filtradas). `user_id=None` (uso admin/CLI) no filtra por dueño."""
    where: list[str] = ["TRUE"]
    params: dict[str, Any] = {}
    if user_id is not None:
        where.append("user_id = :uid")
        params["uid"] = user_id
    if source_type is not None:
        where.append("source_type = :stype")
        params["stype"] = source_type
    if source_id is not None:
        where.append("source_id = :sid")
        params["sid"] = source_id
    if enabled_only:
        where.append("enabled")
    sql = (
        "SELECT id, user_id, source_type, source_id, scope, action, priority, enabled "
        f"FROM filter_rules WHERE {' AND '.join(where)} ORDER BY priority DESC, id ASC"
    )
    rows = conn.execute(text(sql), params).mappings().all()
    return [FilterRule.model_validate(dict(r)) for r in rows]


def get_rule(conn: Connection, rule_id: int, *, user_id: int | None = None) -> FilterRule | None:
    """Una regla por id (opcionalmente acotada al dueño), o None."""
    where = ["id = :id"]
    params: dict[str, Any] = {"id": rule_id}
    if user_id is not None:
        where.append("user_id = :uid")
        params["uid"] = user_id
    row = (
        conn.execute(
            text(
                "SELECT id, user_id, source_type, source_id, scope, action, priority, enabled "
                f"FROM filter_rules WHERE {' AND '.join(where)}"
            ),
            params,
        )
        .mappings()
        .first()
    )
    return FilterRule.model_validate(dict(row)) if row else None


def update_rule(
    conn: Connection,
    rule_id: int,
    *,
    user_id: int | None = None,
    scope: dict[str, Any] | None = None,
    action: FilterAction | None = None,
    priority: int | None = None,
    enabled: bool | None = None,
) -> bool:
    """Update parcial de una regla. Devuelve False si no existe (o no es del `user_id`)."""
    sets: list[str] = []
    params: dict[str, Any] = {"id": rule_id}
    if scope is not None:
        sets.append("scope = CAST(:scope AS JSONB)")
        params["scope"] = json.dumps(scope)
    if action is not None:
        sets.append("action = :action")
        params["action"] = action
    if priority is not None:
        sets.append("priority = :prio")
        params["prio"] = priority
    if enabled is not None:
        sets.append("enabled = :enabled")
        params["enabled"] = enabled
    if not sets:
        return True
    owner = ""
    if user_id is not None:
        owner = " AND user_id = :uid"
        params["uid"] = user_id
    n = conn.execute(
        text(f"UPDATE filter_rules SET {', '.join(sets)} WHERE id = :id{owner}"), params
    ).rowcount
    return n > 0


def delete_rule(conn: Connection, rule_id: int, *, user_id: int | None = None) -> bool:
    """Borra una regla. Devuelve False si no existe (o no es del `user_id`)."""
    params: dict[str, Any] = {"id": rule_id}
    owner = ""
    if user_id is not None:
        owner = " AND user_id = :uid"
        params["uid"] = user_id
    n = conn.execute(text(f"DELETE FROM filter_rules WHERE id = :id{owner}"), params).rowcount
    return n > 0


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
        # `_load` hace un query síncrono a Postgres; lo corremos en threadpool
        # para no bloquear el event loop del listener bajo ráfagas de eventos.
        rules = await asyncio.to_thread(self._load, ctx.user_id, ctx.source_type, ctx.source_id)
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
