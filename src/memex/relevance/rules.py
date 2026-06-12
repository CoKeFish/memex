"""Reglas deterministas del gate (`relevance_gate_rules`) + dry run contra el histórico.

Las reglas las propone la minería LLM (segunda pasada sobre los no-relevantes) o el dueño a
mano, pero NUNCA se activan a ciegas: toda propuesta pasa por `dry_run_rule`, un matcher
determinista contra TODOS los correos históricos del usuario. Si la regla matchearía algún
correo de relevancia efectiva TRUE (mark manual o veredicto `relevant`), está mal hecha →
queda `rejected` CON su reporte (auditoría del porqué). Si pasa, se auto-activa
(`activated_at`) y es reversible (active↔disabled) desde /filtros o CLI.

El matcheo vive DOS veces a propósito y debe mantenerse en espejo:
- `match_rule` (Python): lo aplica el gate sobre los WorkRow pendientes (pre-filtro sin LLM).
- `_MATCH_SQL` (SQL): lo aplica el dry run sobre el histórico completo (sin traer payloads).
Semántica por kind: igualdad exacta case-insensitive (sender_email/sender_domain/list_id);
substring case-insensitive (subject_contains).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from memex.processing.windows import WorkRow
from memex.relevance.verdicts import EMAIL_TYPES

RULE_KINDS = ("sender_email", "sender_domain", "subject_contains", "list_id")
RULE_STATUSES = ("active", "disabled", "rejected")

_ROW_COLS = (
    "id, kind, pattern, status, proposed_by, rationale, dry_run_report, model, "
    "activated_at, deactivated_at, created_at, updated_at"
)

#: Tope de ids de ejemplo (correos relevantes atrapados) que guarda el reporte del dry run.
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


def match_rule(kind: str, pattern: str, fields: EmailFields) -> bool:
    """¿La regla (kind, pattern) matchea estos campos? Espejo Python de `_MATCH_SQL`."""
    p = pattern.strip().lower()
    if not p:
        return False
    if kind == "sender_email":
        return fields.sender_email == p
    if kind == "sender_domain":
        return fields.sender_domain == p
    if kind == "subject_contains":
        return p in fields.subject.lower()
    if kind == "list_id":
        return fields.list_id == p
    raise ValueError(f"kind de regla desconocido: {kind!r}")


def apply_active_rules(conn: Connection, user_id: int, rows: list[WorkRow]) -> dict[int, int]:
    """Aplica las reglas activas a los mensajes pendientes: {inbox_id: rule_id} de la PRIMERA
    regla (más vieja primero, orden estable) que matchea. Sin LLM."""
    if not rows:
        return {}
    rules = (
        conn.execute(
            text(
                "SELECT id, kind, pattern FROM relevance_gate_rules "
                "WHERE user_id = :uid AND status = 'active' ORDER BY id"
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    )
    if not rules:
        return {}
    matched: dict[int, int] = {}
    for row in rows:
        fields = extract_email_fields(row.payload)
        for rule in rules:
            if match_rule(str(rule["kind"]), str(rule["pattern"]), fields):
                matched[row.inbox_id] = int(rule["id"])
                break
    return matched


@dataclass(frozen=True)
class DryRunReport:
    """Resultado del dry run de una regla contra el histórico de correos del usuario."""

    matched: int
    matched_relevant: int
    matched_not_relevant: int
    matched_unverdicted: int
    relevant_sample_ids: tuple[int, ...]

    @property
    def passes(self) -> bool:
        """La regla pasa si NO atrapa ningún correo de relevancia efectiva TRUE."""
        return self.matched_relevant == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "matched_relevant": self.matched_relevant,
            "matched_not_relevant": self.matched_not_relevant,
            "matched_unverdicted": self.matched_unverdicted,
            "relevant_sample_ids": list(self.relevant_sample_ids),
            "passes": self.passes,
        }


def _like_escape(pattern: str) -> str:
    """Escapa los metacaracteres de LIKE (el pattern es literal, no un glob)."""
    return pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


#: Predicado SQL por kind (espejo de `match_rule`). El dry run matchea en SQL para no traer
#: payloads: corre sobre TODO el histórico (no hay limit — es la garantía de la validación).
_MATCH_SQL = {
    "sender_email": "lower(COALESCE(i.payload->'from'->>'email', '')) = lower(:pattern)",
    "sender_domain": (
        "split_part(lower(COALESCE(i.payload->'from'->>'email', '')), '@', 2) = lower(:pattern)"
    ),
    "subject_contains": "COALESCE(i.payload->>'subject', '') ILIKE :like ESCAPE '\\'",
    "list_id": "lower(COALESCE(i.payload->>'list_id', '')) = lower(:pattern)",
}

#: Relevancia EFECTIVA de un mensaje: mark manual si existe, si no veredicto `relevant`.
_EFFECTIVE_RELEVANT_SQL = "COALESCE(rm.is_relevant, rv.verdict = 'relevant', FALSE)"


def dry_run_rule(conn: Connection, user_id: int, kind: str, pattern: str) -> DryRunReport:
    """Corre la regla contra TODOS los correos históricos del usuario, sin efectos.

    Clasifica cada match por relevancia efectiva (mark manual > veredicto del gate). Un solo
    correo relevante atrapado → la regla está mal hecha (`passes=False`).
    """
    if kind not in RULE_KINDS:
        raise ValueError(f"kind de regla desconocido: {kind!r}; válidos: {RULE_KINDS}")
    if not pattern.strip():
        raise ValueError("el pattern no puede ser vacío")
    params: dict[str, Any] = {
        "uid": user_id,
        "email_types": EMAIL_TYPES,
        "pattern": pattern.strip(),
    }
    if kind == "subject_contains":
        params["like"] = f"%{_like_escape(pattern.strip())}%"

    row = (
        conn.execute(
            text(
                f"""
                SELECT
                    COUNT(*) AS matched,
                    COUNT(*) FILTER (WHERE {_EFFECTIVE_RELEVANT_SQL}) AS matched_relevant,
                    COUNT(*) FILTER (
                        WHERE rm.is_relevant IS NULL AND rv.verdict IS NULL
                    ) AS matched_unverdicted,
                    (ARRAY_AGG(i.id ORDER BY i.id)
                        FILTER (WHERE {_EFFECTIVE_RELEVANT_SQL})
                    )[1:{_SAMPLE_IDS_MAX}] AS relevant_sample_ids
                FROM inbox i
                JOIN sources s ON s.id = i.source_id
                LEFT JOIN relevance_marks rm ON rm.inbox_id = i.id
                LEFT JOIN relevance_verdicts rv ON rv.inbox_id = i.id
                WHERE i.user_id = :uid
                  AND s.type = ANY(:email_types)
                  AND {_MATCH_SQL[kind]}
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
    sample = tuple(int(i) for i in (row["relevant_sample_ids"] or []))
    return DryRunReport(
        matched=matched,
        matched_relevant=relevant,
        matched_not_relevant=matched - relevant - unverdicted,
        matched_unverdicted=unverdicted,
        relevant_sample_ids=sample,
    )


def create_rule(
    conn: Connection,
    user_id: int,
    *,
    kind: str,
    pattern: str,
    proposed_by: str,
    report: DryRunReport,
    rationale: str = "",
    model: str | None = None,
) -> dict[str, Any] | None:
    """Persiste una regla con su reporte de dry run: `active` si pasa, `rejected` si no.

    El reporte se guarda SIEMPRE (también el de las rechazadas: es la auditoría del porqué).
    Duplicada (UNIQUE user/kind/pattern) → None (el caller decide: skip en minería, 409 en API).
    """
    status = "active" if report.passes else "rejected"
    row = (
        conn.execute(
            text(
                f"""
                INSERT INTO relevance_gate_rules
                    (user_id, kind, pattern, status, proposed_by, rationale, dry_run_report,
                     model, activated_at)
                VALUES (:uid, :kind, :pattern, :status, :proposed_by, :rationale,
                        CAST(:report AS JSONB), :model,
                        CASE WHEN :status = 'active' THEN NOW() END)
                ON CONFLICT (user_id, kind, pattern) DO NOTHING
                RETURNING {_ROW_COLS}
                """
            ),
            {
                "uid": user_id,
                "kind": kind,
                "pattern": pattern.strip(),
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
    conn: Connection, user_id: int, *, status: str | None = None
) -> list[dict[str, Any]]:
    """Reglas del usuario (todas o por status), más nuevas primero."""
    if status is not None and status not in RULE_STATUSES:
        raise ValueError(f"status inválido: {status!r}; válidos: {RULE_STATUSES}")
    where = "user_id = :uid" + (" AND status = :status" if status is not None else "")
    rows = (
        conn.execute(
            text(f"SELECT {_ROW_COLS} FROM relevance_gate_rules WHERE {where} ORDER BY id DESC"),
            {"uid": user_id, "status": status},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
