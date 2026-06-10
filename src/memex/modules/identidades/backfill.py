"""Backfill de reclasificación org→producto por VOTO de menciones (determinista, sin LLM).

Antes de la 0057 el plegado mandaba toda mención `producto` a una identidad `organizacion`
(Hearthstone-como-org). Este backfill corrige el directorio EXISTENTE: una identidad
`kind='organizacion'` cuya MAYORÍA ESTRICTA de menciones dice `mentioned_kind='producto'` (más de
la mitad del total; el empate conserva org; sin menciones no aparece) se reclasifica a
`kind='producto'`. Las menciones ex-`agente` ya votan producto (las migró la 0057).

Aplicar mueve TODO lo que referencia el kind/slug (espejo parcial de `merge.py` — mismo id, cambia
el slug, mapa en `relations.vertices.IDENTITY_SLUG_BY_KIND`):

  1. `mod_identidades.kind` → 'producto' (+ `updated_at`);
  2. `mod_identidades_mentions.resolved_kind` → 'producto' (las menciones resueltas ahí);
  3. `relation_edges`: src/dst `identidades:org` → `identidades:producto` (sin riesgo de UNIQUE: el
     id no tenía filas con el slug producto);
  4. `relation_cluster_members.member_slug` ídem (sin esto la membresía apunta a un vértice muerto
     y cada build re-crea + poda su arista `miembro_de`);
  5. candidatos de merge PENDIENTES que quedaron cross-kind → `rejected` (decided_by='backfill'):
     `merge_identities` los rechazaría por siempre por kind_mismatch y la FASE 2 re-pagaría el LLM
     en cada corrida.

Residuos aceptados: `counterparty_identity_id` históricos hacia un producto se conservan (el veto
de finanzas es solo hacia adelante); `person_orgs`/`sites` preexistentes quedan (la API ya no
permite crear nuevos contra un producto).

El CLI (`memex-identidades backfill-productos`) es DRY-RUN por default: imprime la lista de
candidatos y no escribe; `--apply` ejecuta todo en una transacción.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.modules.identidades.resolve import KIND_ORG, KIND_PRODUCTO
from memex.relations.vertices import IDENTITY_SLUG_BY_KIND

_log = get_logger("memex.modules.identidades.backfill")

_ORG_SLUG = IDENTITY_SLUG_BY_KIND[KIND_ORG]
_PRODUCTO_SLUG = IDENTITY_SLUG_BY_KIND[KIND_PRODUCTO]


@dataclass(frozen=True)
class ProductCandidate:
    """Una org cuya mayoría de menciones vota producto: `votos_producto` de `votos_total`."""

    id: int
    display_name: str
    votos_producto: int
    votos_total: int


@dataclass
class BackfillStats:
    """Resumen de una aplicación del backfill (filas tocadas por tabla)."""

    reclassified: int = 0
    mentions: int = 0
    edges: int = 0
    cluster_members: int = 0
    merge_candidates_rejected: int = 0


def find_product_candidates(conn: Connection, user_id: int) -> list[ProductCandidate]:
    """Las identidades `organizacion` del user con mayoría ESTRICTA de menciones `producto`
    (2·votos_producto > votos_total). Empate o minoría conserva org; sin menciones no aparece."""
    rows = (
        conn.execute(
            text(
                """
                SELECT i.id, i.display_name,
                       COUNT(*) FILTER (WHERE m.mentioned_kind = 'producto') AS votos_producto,
                       COUNT(*) AS votos_total
                FROM mod_identidades i
                JOIN mod_identidades_mentions m
                  ON m.user_id = i.user_id AND m.resolved_identity_id = i.id
                WHERE i.user_id = :u AND i.kind = 'organizacion'
                GROUP BY i.id, i.display_name
                HAVING 2 * COUNT(*) FILTER (WHERE m.mentioned_kind = 'producto') > COUNT(*)
                ORDER BY i.id
                """
            ),
            {"u": user_id},
        )
        .mappings()
        .all()
    )
    return [
        ProductCandidate(
            id=int(r["id"]),
            display_name=str(r["display_name"]),
            votos_producto=int(r["votos_producto"]),
            votos_total=int(r["votos_total"]),
        )
        for r in rows
    ]


def apply_reclassification(conn: Connection, user_id: int, ids: list[int]) -> BackfillStats:
    """Reclasifica `ids` (orgs del user) a producto y re-apunta menciones, aristas, membresías y
    candidatos de merge cross-kind (ver docstring del módulo). Atómico sobre `conn` (no abre tx
    propia). Idempotente: re-correr con los mismos ids no toca nada (el WHERE kind='organizacion'
    ya no matchea)."""
    stats = BackfillStats()
    if not ids:
        return stats
    p = {"u": user_id, "ids": ids, "org_slug": _ORG_SLUG, "prod_slug": _PRODUCTO_SLUG}

    stats.reclassified = conn.execute(
        text(
            "UPDATE mod_identidades SET kind = 'producto', updated_at = NOW() "
            "WHERE user_id = :u AND id = ANY(:ids) AND kind = 'organizacion'"
        ),
        p,
    ).rowcount
    stats.mentions = conn.execute(
        text(
            "UPDATE mod_identidades_mentions SET resolved_kind = 'producto' "
            "WHERE user_id = :u AND resolved_identity_id = ANY(:ids)"
        ),
        p,
    ).rowcount
    stats.edges = conn.execute(
        text(
            "UPDATE relation_edges SET src_slug = :prod_slug "
            "WHERE user_id = :u AND src_slug = :org_slug AND src_id = ANY(:ids)"
        ),
        p,
    ).rowcount
    stats.edges += conn.execute(
        text(
            "UPDATE relation_edges SET dst_slug = :prod_slug "
            "WHERE user_id = :u AND dst_slug = :org_slug AND dst_id = ANY(:ids)"
        ),
        p,
    ).rowcount
    stats.cluster_members = conn.execute(
        text(
            "UPDATE relation_cluster_members SET member_slug = :prod_slug "
            "WHERE user_id = :u AND member_slug = :org_slug AND member_id = ANY(:ids)"
        ),
        p,
    ).rowcount
    # Candidatos PENDIENTES que la reclasificación dejó cross-kind: cerrarlos acá (el merge los
    # rechazaría por kind_mismatch en CADA corrida y el desempate LLM se re-pagaría por siempre).
    stats.merge_candidates_rejected = conn.execute(
        text(
            """
            UPDATE mod_identidades_merge_candidates mc
            SET status = 'rejected', decided_by = 'backfill', decided_at = NOW(),
                rationale = 'kinds distintos tras reclasificación org→producto'
            FROM mod_identidades a, mod_identidades b
            WHERE mc.user_id = :u AND mc.status = 'candidate'
              AND a.id = mc.identity_a_id AND b.id = mc.identity_b_id
              AND a.kind <> b.kind
              AND (mc.identity_a_id = ANY(:ids) OR mc.identity_b_id = ANY(:ids))
            """
        ),
        p,
    ).rowcount

    _log.info(
        "identidades.backfill_producto.applied",
        user_id=user_id,
        reclassified=stats.reclassified,
        mentions=stats.mentions,
        edges=stats.edges,
        cluster_members=stats.cluster_members,
        merge_candidates_rejected=stats.merge_candidates_rejected,
    )
    return stats
