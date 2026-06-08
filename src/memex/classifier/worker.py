"""Worker de clasificación post-ingest: lee inbox no-clasificado y llena `classifications`.

Server-side: habla con la DB directo (`memex.db`), NO el HTTP client — no es un ingestor,
así que no aplica la regla de aislamiento de ADR-001.

Trackea su progreso por la AUSENCIA de fila en `classifications` (que tiene
`UNIQUE(inbox_id)`), NO por `inbox.processed_at`: ese ciclo de vida lo querrá también el
futuro summarizer, así que el classifier no lo consume. Sin LLM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from memex.classifier.rules import ClassificationResult, classify
from memex.core.sender_tiers import load_overrides, sender_email
from memex.db import connection
from memex.logging import get_logger

_log = get_logger("memex.classifier.worker")

_DEFAULT_LIMIT = 500


@dataclass
class ClassifyStats:
    """Resumen de una corrida: cuántos se escanearon, cuántos se escribieron, por tier."""

    scanned: int = 0
    classified: int = 0
    by_tier: dict[str, int] = field(default_factory=dict)

    def bump_tier(self, tier: str) -> None:
        self.by_tier[tier] = self.by_tier.get(tier, 0) + 1


def _coerce_payload(raw: Any) -> dict[str, Any]:
    """`payload` JSONB suele volver como dict; defensivo ante str u otros."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def run_classification(
    user_id: int,
    *,
    source_id: int | None = None,
    limit: int = _DEFAULT_LIMIT,
    dry_run: bool = False,
) -> ClassifyStats:
    """Clasifica hasta `limit` mensajes no-clasificados del user.

    Idempotente: solo selecciona inbox SIN fila en `classifications`, e inserta con
    `ON CONFLICT (inbox_id) DO NOTHING`. `dry_run` calcula tiers sin escribir.
    """
    stats = ClassifyStats()
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    source_filter = ""
    if source_id is not None:
        source_filter = "AND i.source_id = :sid"
        params["sid"] = source_id

    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT i.id, i.payload
                    FROM inbox i
                    LEFT JOIN classifications c ON c.inbox_id = i.id
                    WHERE i.user_id = :uid
                      AND c.id IS NULL
                      {source_filter}
                    ORDER BY i.id
                    LIMIT :limit
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )

        overrides = load_overrides(conn, user_id)
        for row in rows:
            stats.scanned += 1
            payload = _coerce_payload(row["payload"])
            email = sender_email(payload)
            if email is not None and email in overrides:
                # "No procesar": el usuario forzó el tier de este remitente (acción asistida del
                # sistema de calidad). Gana sobre la heurística determinista; solo afecta mensajes
                # nuevos (este worker corre sobre los no-clasificados).
                result = ClassificationResult(
                    tier=overrides[email],
                    reason="sender_override",
                    metadata={"rule": "sender_override", "sender_email": email},
                )
            else:
                result = classify(payload)
            stats.bump_tier(result.tier)
            if dry_run:
                continue
            written = conn.execute(
                text(
                    """
                    INSERT INTO classifications (user_id, inbox_id, tier, metadata)
                    VALUES (:uid, :iid, :tier, CAST(:metadata AS JSONB))
                    ON CONFLICT (inbox_id) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "uid": user_id,
                    "iid": int(row["id"]),
                    "tier": result.tier,
                    "metadata": json.dumps(result.metadata),
                },
            ).first()
            if written is not None:
                stats.classified += 1

    _log.info(
        "classifier.run.end",
        user_id=user_id,
        source_id=source_id,
        scanned=stats.scanned,
        classified=stats.classified,
        dry_run=dry_run,
        **{f"tier_{tier}": count for tier, count in stats.by_tier.items()},
    )
    return stats
