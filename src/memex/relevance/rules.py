"""Reglas deterministas del gate (`relevance_gate_rules`) + dry run contra el histórico.

Una regla es COMPUESTA (un remitente Y/O un patrón del asunto, combinados con AND) y tiene una
POLARIDAD `effect`:
- `block`: matchea → veredicto `not_relevant` (el correo NO pasa, sin juez ni revisión).
- `allow`: matchea → veredicto `relevant` (el correo ENTRA sin pasar por el juez).

Predicados (al menos uno; las reglas MINEADAS por el LLM llevan los dos):
- remitente: `sender_kind` ('sender_email'|'sender_domain'|'list_id') + `sender_value`.
- asunto: `subject_pattern` (substring case-insensitive).

Las reglas las propone la minería LLM (segunda pasada sobre relevantes/no-relevantes) o el dueño a
mano, pero NUNCA se activan a ciegas: toda propuesta pasa por `dry_run_rule`, un matcher
determinista contra TODOS los correos históricos del usuario. Si una regla `block` matchearía un
correo de relevancia efectiva TRUE (o una `allow`, uno de relevancia FALSE), está mal hecha →
queda `rejected` CON su reporte (auditoría del porqué). Si pasa, se auto-activa (`activated_at`) y
es reversible (active↔disabled) desde /filtros o CLI.

El matcheo vive DOS veces a propósito y debe mantenerse en espejo:
- `rule_matches` (Python): lo aplica el gate sobre los WorkRow pendientes (pre-filtro sin LLM).
- `_SENDER_MATCH_SQL`/`_SUBJECT_MATCH_SQL` (SQL): los aplica el dry run sobre el histórico
  completo (sin traer payloads). Semántica: igualdad exacta case-insensitive del remitente;
  substring case-insensitive del asunto.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from memex.processing.windows import WorkRow
from memex.relevance.verdicts import EMAIL_TYPES

#: Tipos de predicado de remitente (el QUIÉN). El patrón del asunto (el QUÉ) es un slot aparte.
SENDER_KINDS = ("sender_email", "sender_domain", "list_id")
#: Polaridad de una regla.
EFFECTS = ("block", "allow")
RULE_STATUSES = ("active", "disabled", "rejected")

_ROW_COLS = (
    "id, effect, sender_kind, sender_value, subject_pattern, status, proposed_by, rationale, "
    "dry_run_report, model, activated_at, deactivated_at, created_at, updated_at"
)

#: Tope de ids de ejemplo (correos contaminantes) que guarda el reporte del dry run.
_SAMPLE_IDS_MAX = 20


@dataclass(frozen=True)
class EmailFields:
    """Campos normalizados de un correo contra los que matchean las reglas."""

    sender_email: str  # lower; "" si falta
    sender_domain: str  # lower, parte tras @; "" si falta
    subject: str  # crudo (el matcheo baja a lower)
    list_id: str  # lower; "" si falta


def extract_email_fields(payload: dict[str, Any]) -> EmailFields:
    """Normaliza los campos matcheables de un payload de correo (faltantes → "")."""
    from_ = payload.get("from") or {}
    email = str(from_.get("email") or "").strip().lower() if isinstance(from_, dict) else ""
    domain = email.split("@", 1)[1] if "@" in email else ""
    return EmailFields(
        sender_email=email,
        sender_domain=domain,
        subject=str(payload.get("subject") or ""),
        list_id=str(payload.get("list_id") or "").strip().lower(),
    )


def match_sender(sender_kind: str, sender_value: str, fields: EmailFields) -> bool:
    """¿Matchea el predicado de remitente (kind, value)? Espejo de `_sender_match_sql`."""
    v = sender_value.strip().lower()
    if not v:
        return False
    if sender_kind == "sender_email":
        return fields.sender_email == v
    if sender_kind == "sender_domain":
        return fields.sender_domain == v
    if sender_kind == "list_id":
        return fields.list_id == v
    raise ValueError(f"sender_kind desconocido: {sender_kind!r}; válidos: {SENDER_KINDS}")


def match_subject(subject_pattern: str, fields: EmailFields) -> bool:
    """Substring case-insensitive en el asunto. Espejo de `_SUBJECT_MATCH_SQL`."""
    p = subject_pattern.strip().lower()
    if not p:
        return False
    return p in fields.subject.lower()


def rule_matches(
    sender_kind: str | None,
    sender_value: str | None,
    subject_pattern: str | None,
    fields: EmailFields,
) -> bool:
    """¿Matchea la regla compuesta? AND de los predicados PRESENTES (≥1 por el esquema).

    Sin ningún predicado (no debería pasar) → False, defensivo: una regla vacía no matchea todo.
    """
    has_predicate = False
    if sender_kind is not None:
        has_predicate = True
        if not match_sender(sender_kind, sender_value or "", fields):
            return False
    if subject_pattern is not None:
        has_predicate = True
        if not match_subject(subject_pattern, fields):
            return False
    return has_predicate


@dataclass(frozen=True)
class RuleDecision:
    """Cortocircuito determinista de una regla para un mensaje (sin conflicto de polaridad)."""

    inbox_id: int
    effect: str  # 'block' | 'allow'
    rule_id: int


@dataclass(frozen=True)
class RuleConflict:
    """Mensaje matcheado por reglas de AMBAS polaridades: no se cortocircuita, cae al juez."""

    inbox_id: int
    block_rule_id: int
    allow_rule_id: int


@dataclass(frozen=True)
class RuleApplication:
    """Resultado de aplicar las reglas activas a una ventana: decisiones + conflictos."""

    decisions: list[RuleDecision]
    conflicts: list[RuleConflict]


def apply_active_rules(conn: Connection, user_id: int, rows: list[WorkRow]) -> RuleApplication:
    """Aplica las reglas activas a los mensajes pendientes, sin LLM.

    Por mensaje toma la PRIMERA regla block y la PRIMERA allow que matchean (más viejas primero,
    orden estable). Si matchean AMBAS polaridades → conflicto (va al juez, no se cortocircuita);
    si solo una → decisión de esa polaridad; si ninguna → el mensaje queda pendiente (juez).
    """
    if not rows:
        return RuleApplication([], [])
    rules = (
        conn.execute(
            text(
                "SELECT id, effect, sender_kind, sender_value, subject_pattern "
                "FROM relevance_gate_rules "
                "WHERE user_id = :uid AND status = 'active' ORDER BY id"
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    )
    if not rules:
        return RuleApplication([], [])
    decisions: list[RuleDecision] = []
    conflicts: list[RuleConflict] = []
    for row in rows:
        fields = extract_email_fields(row.payload)
        block_id: int | None = None
        allow_id: int | None = None
        for rule in rules:
            sk = None if rule["sender_kind"] is None else str(rule["sender_kind"])
            sv = None if rule["sender_value"] is None else str(rule["sender_value"])
            sp = None if rule["subject_pattern"] is None else str(rule["subject_pattern"])
            if not rule_matches(sk, sv, sp, fields):
                continue
            if rule["effect"] == "block":
                if block_id is None:
                    block_id = int(rule["id"])
            elif allow_id is None:
                allow_id = int(rule["id"])
            if block_id is not None and allow_id is not None:
                break
        if block_id is not None and allow_id is not None:
            conflicts.append(RuleConflict(row.inbox_id, block_id, allow_id))
        elif block_id is not None:
            decisions.append(RuleDecision(row.inbox_id, "block", block_id))
        elif allow_id is not None:
            decisions.append(RuleDecision(row.inbox_id, "allow", allow_id))
    return RuleApplication(decisions, conflicts)


@dataclass(frozen=True)
class DryRunReport:
    """Resultado del dry run de una regla contra el histórico de correos del usuario.

    `passes` depende de la polaridad: una `block` no debe atrapar ningún correo de relevancia
    efectiva TRUE; una `allow`, ninguno de relevancia FALSE. La precisión la da el patrón
    (remitente+asunto carva el subconjunto correcto), no una tolerancia de ratio.
    """

    effect: str
    matched: int
    matched_relevant: int
    matched_not_relevant: int
    matched_unverdicted: int
    relevant_sample_ids: tuple[int, ...]
    not_relevant_sample_ids: tuple[int, ...]

    @property
    def passes(self) -> bool:
        if self.effect == "allow":
            return self.matched_not_relevant == 0
        return self.matched_relevant == 0

    @property
    def contaminating_sample_ids(self) -> tuple[int, ...]:
        """Los ids que hacen la regla insegura: relevantes para block, no-relevantes para allow."""
        return self.not_relevant_sample_ids if self.effect == "allow" else self.relevant_sample_ids

    def as_dict(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "matched": self.matched,
            "matched_relevant": self.matched_relevant,
            "matched_not_relevant": self.matched_not_relevant,
            "matched_unverdicted": self.matched_unverdicted,
            "relevant_sample_ids": list(self.relevant_sample_ids),
            "not_relevant_sample_ids": list(self.not_relevant_sample_ids),
            "contaminating_sample_ids": list(self.contaminating_sample_ids),
            "passes": self.passes,
        }


def _like_escape(pattern: str) -> str:
    """Escapa los metacaracteres de LIKE (el pattern es literal, no un glob)."""
    return pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sender_match_sql(sender_kind: str) -> str:
    """Predicado SQL de remitente por kind (espejo de `match_sender`)."""
    if sender_kind == "sender_email":
        return "lower(COALESCE(i.payload->'from'->>'email', '')) = :sender_value"
    if sender_kind == "sender_domain":
        return (
            "split_part(lower(COALESCE(i.payload->'from'->>'email', '')), '@', 2) = :sender_value"
        )
    if sender_kind == "list_id":
        return "lower(COALESCE(i.payload->>'list_id', '')) = :sender_value"
    raise ValueError(f"sender_kind desconocido: {sender_kind!r}; válidos: {SENDER_KINDS}")


#: Predicado SQL de asunto (espejo de `match_subject`).
_SUBJECT_MATCH_SQL = "COALESCE(i.payload->>'subject', '') ILIKE :subject_like ESCAPE '\\'"

#: Relevancia EFECTIVA de un mensaje: mark manual si existe, si no veredicto `relevant`.
_EFFECTIVE_RELEVANT_SQL = "COALESCE(rm.is_relevant, rv.verdict = 'relevant', FALSE)"
#: ¿El mensaje tiene señal (mark o veredicto)? Sin señal = pendiente, no cuenta de ningún lado.
_HAS_SIGNAL_SQL = "(rm.is_relevant IS NOT NULL OR rv.verdict IS NOT NULL)"


def validate_predicates(
    sender_kind: str | None, sender_value: str | None, subject_pattern: str | None
) -> tuple[str | None, str | None, str | None]:
    """Valida y NORMALIZA un set de predicados de regla (≥1, remitente y valor van juntos).

    Devuelve `(sender_kind, sender_value, subject_pattern)` normalizados: `sender_value` a lower
    (los remitentes son case-insensitive), `subject_pattern` trim conservando mayúsculas (display;
    el matcheo igual baja a lower). `ValueError` si el set es inválido.
    """
    sk = (sender_kind or "").strip() or None
    sv = (sender_value or "").strip()
    sp = (subject_pattern or "").strip()
    if sk is not None and sk not in SENDER_KINDS:
        raise ValueError(f"sender_kind inválido: {sk!r}; válidos: {SENDER_KINDS}")
    if sk is not None and not sv:
        raise ValueError("sender_value requerido cuando hay sender_kind")
    if sk is None and sv:
        raise ValueError("sender_kind requerido cuando hay sender_value")
    sv_norm = sv.lower() if sk is not None else None
    sp_norm = sp or None
    if sv_norm is None and sp_norm is None:
        raise ValueError("una regla necesita al menos un predicado (remitente o asunto)")
    return sk, sv_norm, sp_norm


def dry_run_rule(
    conn: Connection,
    user_id: int,
    *,
    effect: str,
    sender_kind: str | None = None,
    sender_value: str | None = None,
    subject_pattern: str | None = None,
) -> DryRunReport:
    """Corre la regla compuesta contra TODOS los correos históricos del usuario, sin efectos.

    Clasifica cada match por relevancia efectiva (mark manual > veredicto del gate). Según la
    polaridad, un solo correo del lado contaminante → la regla está mal hecha (`passes=False`).
    """
    if effect not in EFFECTS:
        raise ValueError(f"effect inválido: {effect!r}; válidos: {EFFECTS}")
    sk, sv, sp = validate_predicates(sender_kind, sender_value, subject_pattern)

    clauses: list[str] = []
    params: dict[str, Any] = {"uid": user_id, "email_types": EMAIL_TYPES}
    if sk is not None:
        clauses.append(_sender_match_sql(sk))
        params["sender_value"] = sv
    if sp is not None:
        clauses.append(_SUBJECT_MATCH_SQL)
        params["subject_like"] = f"%{_like_escape(sp)}%"
    predicate_sql = " AND ".join(f"({c})" for c in clauses)

    not_relevant_sql = f"({_HAS_SIGNAL_SQL} AND NOT {_EFFECTIVE_RELEVANT_SQL})"
    row = (
        conn.execute(
            text(
                f"""
                SELECT
                    COUNT(*) AS matched,
                    COUNT(*) FILTER (WHERE {_EFFECTIVE_RELEVANT_SQL}) AS matched_relevant,
                    COUNT(*) FILTER (WHERE NOT {_HAS_SIGNAL_SQL}) AS matched_unverdicted,
                    (ARRAY_AGG(i.id ORDER BY i.id)
                        FILTER (WHERE {_EFFECTIVE_RELEVANT_SQL})
                    )[1:{_SAMPLE_IDS_MAX}] AS relevant_sample_ids,
                    (ARRAY_AGG(i.id ORDER BY i.id)
                        FILTER (WHERE {not_relevant_sql})
                    )[1:{_SAMPLE_IDS_MAX}] AS not_relevant_sample_ids
                FROM inbox i
                JOIN sources s ON s.id = i.source_id
                LEFT JOIN relevance_marks rm ON rm.inbox_id = i.id
                LEFT JOIN relevance_verdicts rv ON rv.inbox_id = i.id
                WHERE i.user_id = :uid
                  AND s.type = ANY(:email_types)
                  AND {predicate_sql}
                """
            ),
            params,
        )
        .mappings()
        .one()
    )
    matched = int(row["matched"])
    relevant = int(row["matched_relevant"])
    unverdicted = int(row["matched_unverdicted"])
    return DryRunReport(
        effect=effect,
        matched=matched,
        matched_relevant=relevant,
        matched_not_relevant=matched - relevant - unverdicted,
        matched_unverdicted=unverdicted,
        relevant_sample_ids=tuple(int(i) for i in (row["relevant_sample_ids"] or [])),
        not_relevant_sample_ids=tuple(int(i) for i in (row["not_relevant_sample_ids"] or [])),
    )


def create_rule(
    conn: Connection,
    user_id: int,
    *,
    effect: str,
    sender_kind: str | None = None,
    sender_value: str | None = None,
    subject_pattern: str | None = None,
    proposed_by: str,
    report: DryRunReport,
    rationale: str = "",
    model: str | None = None,
) -> dict[str, Any] | None:
    """Persiste una regla compuesta con su reporte de dry run: `active` si pasa, `rejected` si no.

    El reporte se guarda SIEMPRE (también el de las rechazadas: es la auditoría del porqué).
    Duplicada (mismo user/effect/predicados, case-insensitive) → None (el caller decide: skip en
    minería, 409 en API).
    """
    if effect not in EFFECTS:
        raise ValueError(f"effect inválido: {effect!r}; válidos: {EFFECTS}")
    sk, sv, sp = validate_predicates(sender_kind, sender_value, subject_pattern)
    status = "active" if report.passes else "rejected"
    row = (
        conn.execute(
            text(
                f"""
                INSERT INTO relevance_gate_rules
                    (user_id, effect, sender_kind, sender_value, subject_pattern, status,
                     proposed_by, rationale, dry_run_report, model, activated_at)
                VALUES (:uid, :effect, :sender_kind, :sender_value, :subject_pattern, :status,
                        :proposed_by, :rationale, CAST(:report AS JSONB), :model,
                        CASE WHEN :status = 'active' THEN NOW() END)
                ON CONFLICT (
                    user_id, effect, lower(coalesce(sender_kind, '')),
                    lower(coalesce(sender_value, '')), lower(coalesce(subject_pattern, ''))
                ) DO NOTHING
                RETURNING {_ROW_COLS}
                """
            ),
            {
                "uid": user_id,
                "effect": effect,
                "sender_kind": sk,
                "sender_value": sv,
                "subject_pattern": sp,
                "status": status,
                "proposed_by": proposed_by,
                "rationale": rationale[:1000],
                "report": json.dumps(report.as_dict()),
                "model": model,
            },
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def set_rule_status(
    conn: Connection, rule_id: int, user_id: int, status: str
) -> dict[str, Any] | None:
    """Toggle reversible active↔disabled. Una regla `rejected` no se puede activar (falló su
    dry run; si el dueño la quiere, la crea a mano y el dry run corre de nuevo). None si no
    existe, ValueError si la transición no es válida."""
    if status not in ("active", "disabled"):
        raise ValueError(f"status inválido: {status!r}; válidos: ('active', 'disabled')")
    row = (
        conn.execute(
            text(
                f"""
                UPDATE relevance_gate_rules
                SET status = :status,
                    activated_at = CASE WHEN :status = 'active' THEN NOW() ELSE activated_at END,
                    deactivated_at = CASE WHEN :status = 'disabled' THEN NOW()
                                          ELSE deactivated_at END,
                    updated_at = NOW()
                WHERE id = :id AND user_id = :uid AND status <> 'rejected'
                RETURNING {_ROW_COLS}
                """
            ),
            {"status": status, "id": rule_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def list_rules(
    conn: Connection, user_id: int, *, status: str | None = None, effect: str | None = None
) -> list[dict[str, Any]]:
    """Reglas del usuario (todas o por status/effect), más nuevas primero."""
    if status is not None and status not in RULE_STATUSES:
        raise ValueError(f"status inválido: {status!r}; válidos: {RULE_STATUSES}")
    if effect is not None and effect not in EFFECTS:
        raise ValueError(f"effect inválido: {effect!r}; válidos: {EFFECTS}")
    where = "user_id = :uid"
    if status is not None:
        where += " AND status = :status"
    if effect is not None:
        where += " AND effect = :effect"
    rows = (
        conn.execute(
            text(f"SELECT {_ROW_COLS} FROM relevance_gate_rules WHERE {where} ORDER BY id DESC"),
            {"uid": user_id, "status": status, "effect": effect},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
