"""`merge_identities` — funde dos identidades canónicas en una (primitiva del auto-merge + LLM).

Cuando el difuso (alto) o el LLM (zona gris) deciden que dos filas de `mod_identidades` son la MISMA
identidad, esta primitiva las colapsa en la superviviente y borra la absorbida, sin perder señales:

  1. mueve identificadores (ON CONFLICT DO NOTHING) y sedes a la superviviente;
  2. re-apunta afiliaciones (person_orgs) evitando duplicar la UNIQUE(person_id, org_id);
  3. re-apunta las menciones (`resolved_identity_id`);
  4. re-apunta las aristas del grafo (`relation_edges`, src y dst del slug del kind), colapsando las
     que violarían `relation_edges_logical_uq` y las que quedarían self-loop;
  4b. re-apunta la jerarquía de pertenencia: los hijos del absorbido cuelgan del superviviente y se
     limpia el self-loop si el superviviente colgaba del absorbido (el padre final se decide abajo);
  4c. re-apunta el `counterparty_identity_id` de finanzas (consolidado + crudas): el FK es ON DELETE
     SET NULL, así que sin esto el DELETE de la absorbida perdería el vínculo finance↔identidad;
  5. agrega el nombre + alias de la absorbida a los alias de la superviviente;
  6. fill-only de columnas NULL de la superviviente (given/family/birthday/foto/provider*/notes y el
     `parent_identity_id`, sin crear self-parent);
  7. deja auditoría en `metadata.merged_from` y borra la absorbida.

Solo funde identidades del MISMO `user_id` y MISMO `kind` (persona con persona, org con org).
Atómica sobre `conn` (no abre tx propia). Devuelve True si fundió (False si algún id no existe).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger

_log = get_logger("memex.modules.identidades.merge")

#: slug del grafo por kind (espejo de relations/vertices.NODE_SOURCES).
_SLUG_BY_KIND = {"persona": "identidades:person", "organizacion": "identidades:org"}


def merge_identities(conn: Connection, user_id: int, survivor_id: int, absorbed_id: int) -> bool:
    """Funde `absorbed_id` en `survivor_id` (mismo user + mismo kind). Idempotente respecto a las
    UNIQUE de identifiers/afiliaciones/aristas. Devuelve True si fundió."""
    if survivor_id == absorbed_id:
        return False
    rows = (
        conn.execute(
            text(
                "SELECT id, kind, display_name FROM mod_identidades "
                "WHERE user_id = :u AND id = ANY(:ids)"
            ),
            {"u": user_id, "ids": [survivor_id, absorbed_id]},
        )
        .mappings()
        .all()
    )
    by_id = {int(r["id"]): r for r in rows}
    if survivor_id not in by_id or absorbed_id not in by_id:
        return False
    if by_id[survivor_id]["kind"] != by_id[absorbed_id]["kind"]:
        _log.warning("identidades.merge.kind_mismatch", survivor=survivor_id, absorbed=absorbed_id)
        return False
    slug = _SLUG_BY_KIND[str(by_id[survivor_id]["kind"])]
    p = {"u": user_id, "surv": survivor_id, "absb": absorbed_id, "slug": slug}

    # 1. identificadores (mover sin duplicar) + sedes.
    conn.execute(
        text(
            """
            INSERT INTO mod_identidades_identifiers
              (user_id, identity_id, platform, kind, value, value_norm,
               is_primary, source, metadata)
            SELECT user_id, :surv, platform, kind, value, value_norm, FALSE, source, metadata
            FROM mod_identidades_identifiers WHERE identity_id = :absb
            ON CONFLICT (identity_id, platform, kind, value_norm) DO NOTHING
            """
        ),
        p,
    )
    conn.execute(text("DELETE FROM mod_identidades_identifiers WHERE identity_id = :absb"), p)
    conn.execute(
        text("UPDATE mod_identidades_sites SET identity_id = :surv WHERE identity_id = :absb"), p
    )

    # 2. afiliaciones: mismo kind → la absorbida aparece en una sola columna (person_id si persona,
    #    org_id si org). Se borran las que colisionarían con la UNIQUE(person_id, org_id) y se
    #    re-apunta el resto. Ambos pares son no-op para la columna que no aplica.
    conn.execute(
        text(
            """
            DELETE FROM mod_identidades_person_orgs a
            WHERE a.person_id = :absb AND EXISTS (
              SELECT 1 FROM mod_identidades_person_orgs s
              WHERE s.person_id = :surv AND s.org_id = a.org_id)
            """
        ),
        p,
    )
    conn.execute(
        text("UPDATE mod_identidades_person_orgs SET person_id = :surv WHERE person_id = :absb"), p
    )
    conn.execute(
        text(
            """
            DELETE FROM mod_identidades_person_orgs a
            WHERE a.org_id = :absb AND EXISTS (
              SELECT 1 FROM mod_identidades_person_orgs s
              WHERE s.org_id = :surv AND s.person_id = a.person_id)
            """
        ),
        p,
    )
    conn.execute(
        text("UPDATE mod_identidades_person_orgs SET org_id = :surv WHERE org_id = :absb"), p
    )

    # 3. menciones.
    conn.execute(
        text(
            "UPDATE mod_identidades_mentions SET resolved_identity_id = :surv "
            "WHERE resolved_identity_id = :absb"
        ),
        p,
    )

    # 4. aristas del grafo. Primero borrar las que quedarían self-loop (absorbida↔superviviente en
    #    el mismo slug), luego las que colisionarían con la UNIQUE lógica de la superviviente, luego
    #    re-apuntar src y dst.
    conn.execute(
        text(
            """
            DELETE FROM relation_edges
            WHERE user_id = :u
              AND ((src_slug = :slug AND src_id = :absb AND dst_slug = :slug AND dst_id = :surv)
                OR (src_slug = :slug AND src_id = :surv AND dst_slug = :slug AND dst_id = :absb))
            """
        ),
        p,
    )
    conn.execute(
        text(
            """
            DELETE FROM relation_edges a
            WHERE a.user_id = :u AND a.src_slug = :slug AND a.src_id = :absb AND EXISTS (
              SELECT 1 FROM relation_edges s
              WHERE s.user_id = :u AND s.src_slug = :slug AND s.src_id = :surv
                AND s.dst_slug = a.dst_slug AND s.dst_id = a.dst_id
                AND s.relation_type = a.relation_type AND s.producer = a.producer)
            """
        ),
        p,
    )
    conn.execute(
        text(
            "UPDATE relation_edges SET src_id = :surv "
            "WHERE user_id = :u AND src_slug = :slug AND src_id = :absb"
        ),
        p,
    )
    conn.execute(
        text(
            """
            DELETE FROM relation_edges a
            WHERE a.user_id = :u AND a.dst_slug = :slug AND a.dst_id = :absb AND EXISTS (
              SELECT 1 FROM relation_edges s
              WHERE s.user_id = :u AND s.dst_slug = :slug AND s.dst_id = :surv
                AND s.src_slug = a.src_slug AND s.src_id = a.src_id
                AND s.relation_type = a.relation_type AND s.producer = a.producer)
            """
        ),
        p,
    )
    conn.execute(
        text(
            "UPDATE relation_edges SET dst_id = :surv "
            "WHERE user_id = :u AND dst_slug = :slug AND dst_id = :absb"
        ),
        p,
    )

    # 4b. jerarquía de pertenencia: los hijos del absorbido cuelgan del superviviente; si el
    #     superviviente colgaba del absorbido, ese link queda self-loop → se limpia (el fill-only de
    #     abajo decide el padre final del superviviente con la guarda anti self-parent).
    conn.execute(
        text(
            "UPDATE mod_identidades SET parent_identity_id = :surv "
            "WHERE parent_identity_id = :absb AND user_id = :u AND id <> :surv"
        ),
        p,
    )
    conn.execute(
        text(
            "UPDATE mod_identidades SET parent_identity_id = NULL "
            "WHERE id = :surv AND parent_identity_id = :absb AND user_id = :u"
        ),
        p,
    )

    # 4c. costura finanzas: re-apuntar `counterparty_identity_id` absorbido→superviviente ANTES del
    #     DELETE. El FK es ON DELETE SET NULL (0036): sin esto, borrar el absorbido perdería en
    #     silencio el vínculo finance↔identidad (la consolidación re-escribiría NULL). Sin UNIQUE
    #     en esa columna → sin manejo de conflicto. (Follow-up: centralizar las FK SET NULL.)
    conn.execute(
        text(
            "UPDATE mod_finance_consolidated SET counterparty_identity_id = :surv "
            "WHERE user_id = :u AND counterparty_identity_id = :absb"
        ),
        p,
    )
    conn.execute(
        text(
            "UPDATE mod_finance_transactions SET counterparty_identity_id = :surv "
            "WHERE user_id = :u AND counterparty_identity_id = :absb"
        ),
        p,
    )

    # 5/6/7. alias (nombre + alias de la absorbida), fill-only de columnas NULL, auditoría.
    conn.execute(
        text(
            """
            UPDATE mod_identidades surv SET
              aliases = (
                SELECT COALESCE(array_agg(DISTINCT x), '{}')
                FROM unnest(surv.aliases || absb.aliases || ARRAY[absb.display_name]) AS x
                WHERE x <> surv.display_name
              ),
              given_name  = COALESCE(surv.given_name, absb.given_name),
              family_name = COALESCE(surv.family_name, absb.family_name),
              birthday    = COALESCE(surv.birthday, absb.birthday),
              photo_url   = COALESCE(surv.photo_url, absb.photo_url),
              notes       = CASE WHEN btrim(surv.notes) = '' THEN absb.notes ELSE surv.notes END,
              provider               = CASE WHEN surv.provider_resource_name IS NULL
                                            THEN absb.provider ELSE surv.provider END,
              provider_account_id    = CASE WHEN surv.provider_resource_name IS NULL
                                            THEN absb.provider_account_id
                                            ELSE surv.provider_account_id END,
              provider_resource_name = COALESCE(surv.provider_resource_name,
                                                absb.provider_resource_name),
              provider_etag          = CASE WHEN surv.provider_resource_name IS NULL
                                            THEN absb.provider_etag ELSE surv.provider_etag END,
              interest    = surv.interest OR absb.interest,
              parent_identity_id = CASE
                              WHEN surv.parent_identity_id IS NOT NULL
                                   THEN surv.parent_identity_id
                              WHEN absb.parent_identity_id = surv.id THEN NULL
                              ELSE absb.parent_identity_id END,
              metadata    = jsonb_set(
                              surv.metadata, '{merged_from}',
                              COALESCE(surv.metadata->'merged_from', '[]'::jsonb)
                                || to_jsonb(:absb)),
              updated_at  = NOW()
            FROM mod_identidades absb
            WHERE surv.id = :surv AND absb.id = :absb
            """
        ),
        p,
    )
    conn.execute(text("DELETE FROM mod_identidades WHERE id = :absb AND user_id = :u"), p)
    _log.info("identidades.merge.done", survivor=survivor_id, absorbed=absorbed_id)
    return True
