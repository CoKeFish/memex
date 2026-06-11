"""Lookup read-only de resúmenes por inbox: la vía para que OTROS subsistemas consuman el
resumen ya pagado de un mensaje (p.ej. el resolver par-por-par lo inyecta como contexto
auxiliar del veredicto).

A diferencia de los helpers del worker (que abren su propia conexión), acá el contrato es
`conn`-first: el caller maneja la transacción. La UNIQUE(inbox_id) de `summary_inbox_links`
(migración 0007) garantiza a lo sumo UN resumen por mensaje — sin desempate. Un inbox purgado
pierde sus links por cascade: el resumen huérfano deja de ser alcanzable por esta vía (el
resumen NUNCA sustituye al original).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True)
class InboxSummary:
    """El resumen vigente de un mensaje: la fila de `summaries` que lo linkea."""

    summary_id: int
    tier: str  #: 'batch' | 'individual'
    content: str
    n: int  #: tamaño del lote que cubre el resumen (metadata->>'n'; 1 si falta)


def summaries_for_inboxes(
    conn: Connection, user_id: int, inbox_ids: Iterable[int]
) -> dict[int, InboxSummary]:
    """Los resúmenes vigentes de los mensajes pedidos, en bloque ({} para input vacío).
    Mensajes sin resumen no aparecen. Un resumen `batch` cubre n mensajes: la MISMA fila
    aparece bajo cada inbox_id linkeado."""
    ids = sorted(set(inbox_ids))
    if not ids:
        return {}
    rows = conn.execute(
        text(
            """
            SELECT sl.inbox_id, s.id, s.tier, s.content,
                   COALESCE((s.metadata->>'n')::int, 1) AS n
            FROM summaries s
            JOIN summary_inbox_links sl ON sl.summary_id = s.id
            WHERE s.user_id = :u AND sl.inbox_id = ANY(:ids)
            """
        ),
        {"u": user_id, "ids": ids},
    ).all()
    return {
        int(ibx): InboxSummary(summary_id=int(sid), tier=str(tier), content=str(content), n=int(n))
        for ibx, sid, tier, content, n in rows
    }
