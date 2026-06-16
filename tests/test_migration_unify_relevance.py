"""Schema check de la migración 0071 (relevancia unificada).

Verifica el DDL que sostiene el rediseño «un solo sistema»: candidatos por-procedimiento
(UNIQUE user_id+procedure+sender_key, sin `llm_verdict`), el dial de costo estrechado
(`sender_tier_overrides` ya no acepta `blacklist`) y la tabla `interest_suggestions`
(segundo lazo) con su dedupe de propuestas pendientes.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.db import connection


def _insert_candidate(procedure: str, sender_key: str) -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO relevance_candidates
                    (user_id, sender_key, sender_label, messages, relevant, inert, procedure)
                VALUES (1, :key, :key, 10, 0, 10, :proc)
                """
            ),
            {"key": sender_key, "proc": procedure},
        )


def test_candidates_unique_per_procedure_and_sender() -> None:
    # Mismo remitente marcado por DOS procedimientos distintos: convive (filas independientes).
    _insert_candidate("sender_relevance", "promos@tienda.com")
    _insert_candidate("fact_count", "promos@tienda.com")
    with connection() as c:
        n = c.execute(
            text("SELECT count(*) FROM relevance_candidates WHERE sender_key = :k"),
            {"k": "promos@tienda.com"},
        ).scalar()
    assert n == 2
    # El mismo (procedure, sender_key) sí choca contra el UNIQUE nuevo.
    with pytest.raises(IntegrityError):
        _insert_candidate("fact_count", "promos@tienda.com")


def test_candidates_llm_verdict_column_removed() -> None:
    with connection() as c:
        cols = {
            r[0]
            for r in c.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'relevance_candidates'"
                )
            )
        }
    assert "llm_verdict" not in cols  # el juez advisory se retiró
    assert {"procedure", "unit_type"} <= cols


def test_sender_tier_overrides_rejects_blacklist() -> None:
    # El override quedó SOLO como dial de costo: blacklist («no procesar») ahora es regla del gate.
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO sender_tier_overrides (user_id, sender_email, tier) "
                "VALUES (1, 'vip@bank.com', 'individual')"
            )
        )
    with pytest.raises(IntegrityError), connection() as c:
        c.execute(
            text(
                "INSERT INTO sender_tier_overrides (user_id, sender_email, tier) "
                "VALUES (1, 'spam@ads.com', 'blacklist')"
            )
        )


def test_gate_settings_accepts_deepseek_provider() -> None:
    # 0067 creó el CHECK con ('anthropic','codex'); 0071 lo ensancha → deepseek de primera clase.
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO relevance_gate_settings (user_id, provider) VALUES (1, 'deepseek') "
                "ON CONFLICT (user_id) DO UPDATE SET provider = 'deepseek'"
            )
        )
    with pytest.raises(IntegrityError), connection() as c:
        c.execute(
            text("INSERT INTO relevance_gate_settings (user_id, provider) VALUES (2, 'openai')")
        )


def test_interest_suggestions_checks_and_pending_dedupe() -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO interest_suggestions (user_id, action, text, rationale) "
                "VALUES (1, 'add', 'descuentos de Steam', 'rescató 6 correos')"
            )
        )
    # action fuera del CHECK
    with pytest.raises(IntegrityError), connection() as c:
        c.execute(
            text(
                "INSERT INTO interest_suggestions (user_id, action, text) VALUES (1, 'tweak', 'x')"
            )
        )
    # dedupe de pendientes: misma (acción, texto en minúsculas) con status 'proposed' → choca
    with pytest.raises(IntegrityError), connection() as c:
        c.execute(
            text(
                "INSERT INTO interest_suggestions (user_id, action, text) "
                "VALUES (1, 'add', 'Descuentos de Steam')"
            )
        )
    # resolver la pendiente libera el dedupe (otra propuesta del mismo texto puede entrar)
    with connection() as c:
        c.execute(text("UPDATE interest_suggestions SET status = 'rejected' WHERE user_id = 1"))
        c.execute(
            text(
                "INSERT INTO interest_suggestions (user_id, action, text) "
                "VALUES (1, 'add', 'descuentos de Steam')"
            )
        )
