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
  4d. re-apunta la membresía de cúmulos (`relation_cluster_members`); si el superviviente ya era
     miembro del mismo cúmulo, la fila de la absorbida se borra (gana la del superviviente,
     incluido su `pruned`);
  5. agrega el nombre + alias de la absorbida a los alias de la superviviente;
  6. fill-only de columnas NULL de la superviviente (given/family/birthday/foto/provider*/notes y el
     `parent_identity_id`, sin crear self-parent);
  7. deja auditoría en `metadata.merged_from` y borra la absorbida.

Funde identidades del MISMO `user_id`. Por defecto same-kind (el path automático —fuzzy/zona
gris— solo propone pares del mismo tipo); ADEMÁS admite CROSS-KIND cuando un caller lo pide
explícito (el resolvedor contextual; el merge manual): la misma entidad real tipada distinto
—típico, una `desconocido` que resulta ser una org ya listada—. Invariante: `desconocido` NUNCA
absorbe a un tipo definido (si el superviviente propuesto es `desconocido` y el absorbido tiene
tipo, se invierten). En cross-kind las aristas del absorbido (de su propio slug) se DESCARTAN y se
marca dirty al vecindario (no se trasladan: `merge_vertices` es same-slug); same-kind las mueve.
Atómica sobre `conn` (no abre tx propia). Devuelve True si fundió (False si algún id no existe).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.modules.identidades.links import repoint_claims
from memex.relations.edges import Ref
from memex.relations.graph_writer import delete_vertex, mark_dirty, merge_vertices
from memex.relations.vertices import IDENTITY_SLUG_BY_KIND

_log = get_logger("memex.modules.identidades.merge")


def merge_identities(conn: Connection, user_id: int, survivor_id: int, absorbed_id: int) -> bool:
    """Funde `absorbed_id` en `survivor_id` (mismo user; same-kind o cross-kind). Idempotente
    respecto a las UNIQUE de identifiers/afiliaciones/aristas. Devuelve True si fundió."""
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
    surv_kind = str(by_id[survivor_id]["kind"])
    absb_kind = str(by_id[absorbed_id]["kind"])
    # Invariante: `desconocido` NUNCA absorbe a un tipo definido. Si el superviviente propuesto es
    # `desconocido` y el absorbido tiene tipo, se INVIERTEN (gana el conocido) — así el cross-kind
    # no degrada una identidad tipada a `desconocido`.
    if surv_kind == "desconocido" and absb_kind != "desconocido":
        survivor_id, absorbed_id = absorbed_id, survivor_id
        surv_kind, absb_kind = absb_kind, surv_kind
    cross_kind = surv_kind != absb_kind
    surv_slug = IDENTITY_SLUG_BY_KIND[surv_kind]
    absb_slug = IDENTITY_SLUG_BY_KIND[absb_kind]
    if cross_kind:
        # señal de monitoreo del experimento «merges entre tipos» (ver si genera ruido para revert).
        _log.warning(
            "identidades.merge.cross_kind",
            survivor=survivor_id,
            absorbed=absorbed_id,
            survivor_kind=surv_kind,
            absorbed_kind=absb_kind,
        )
    p = {"u": user_id, "surv": survivor_id, "absb": absorbed_id}

    # 1. identificadores: re-apuntar del absorbido al superviviente. Se DESCARTAN primero los que el
    #    superviviente YA tiene (per-ficha dup, p.ej. handles compartidos) y se re-apunta el resto
    #    por UPDATE — NO INSERT+DELETE: así no hay un instante con DOS filas del mismo identificador
    #    fuerte (email/phone/domain), que violaría el índice único global (0081). + sedes.
    conn.execute(
        text(
            """
            DELETE FROM mod_identidades_identifiers a
            WHERE a.identity_id = :absb AND EXISTS (
              SELECT 1 FROM mod_identidades_identifiers s
              WHERE s.identity_id = :surv AND s.platform = a.platform
                AND s.kind = a.kind AND s.value_norm = a.value_norm)
            """
        ),
        p,
    )
    conn.execute(
        text(
            "UPDATE mod_identidades_identifiers SET identity_id = :surv, is_primary = FALSE "
            "WHERE identity_id = :absb"
        ),
        p,
    )
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
    # self-afiliación: al fundir tipos distintos el absorbido podía estar afiliado al superviviente
    # (p.ej. una `desconocido` afiliada a la org en la que se funde) → tras re-apuntar quedaría
    # (person_id = org_id = superviviente). Se limpia (no-op en same-kind).
    conn.execute(
        text(
            "DELETE FROM mod_identidades_person_orgs "
            "WHERE user_id = :u AND person_id = :surv AND org_id = :surv"
        ),
        p,
    )

    # 2b. libro de vínculos: los reclamos del absorbido pasan al superviviente (sin duplicar los
    #     iguales; sin self-links). Así la evidencia de «FIB» y «Blockchain Acceleration Foundation»
    #     se suma al fundirlas en vez de perderse con el DELETE en cascada.
    repoint_claims(conn, user_id, survivor_id, absorbed_id)
    # 3. menciones.
    conn.execute(
        text(
            "UPDATE mod_identidades_mentions SET resolved_identity_id = :surv "
            "WHERE resolved_identity_id = :absb"
        ),
        p,
    )

    # 4 + 4d. grafo y membresía de cúmulos vía el GraphWriter (único punto de mutación del grafo),
    #    capturando los ex-vecinos del absorbido y marcándolos `dirty` (groundwork ADR-021).
    if cross_kind:
        # El absorbido vive bajo OTRO slug: sus aristas son de su tipo y `merge_vertices` es
        # same-slug → se BORRA su vértice (captura + marca dirty a sus vecinos) y se marca dirty al
        # superviviente (absorbió identificadores/menciones). Lo stale lo reconsidera el dirty.
        delete_vertex(conn, user_id, Ref(absb_slug, absorbed_id))
        mark_dirty(conn, user_id, [Ref(surv_slug, survivor_id)])
    else:
        merge_vertices(
            conn,
            user_id,
            absorbed=Ref(surv_slug, absorbed_id),
            survivor=Ref(surv_slug, survivor_id),
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

    # 8. anti-ciclo de jerarquía. El re-apunte 4b (hijos del absorbido → superviviente) más el
    #    fill-only del padre pueden cerrar un ciclo al fundir un ANCESTRO dentro de un DESCENDIENTE:
    #    el padre del absorbido pasa a colgar del superviviente, que ya descendía de él. El CHECK de
    #    la DB solo atrapa el self-loop directo, así que el ciclo multinivel se rompe acá con
    #    `would_create_cycle` (import local: evita el ciclo de import merge↔hierarchy). Toda cadena
    #    de jerarquía que este merge pudo cerrar pasa por el superviviente, así que anular su padre
    #    la corta. Idempotente (no-op si no hay ciclo).
    from memex.modules.identidades.hierarchy import would_create_cycle

    surv_parent = conn.execute(
        text("SELECT parent_identity_id FROM mod_identidades WHERE id = :surv AND user_id = :u"), p
    ).scalar()
    if surv_parent is not None and would_create_cycle(conn, user_id, survivor_id, int(surv_parent)):
        conn.execute(
            text(
                "UPDATE mod_identidades SET parent_identity_id = NULL "
                "WHERE id = :surv AND user_id = :u"
            ),
            p,
        )
        _log.info(
            "identidades.merge.broke_parent_cycle", survivor=survivor_id, absorbed=absorbed_id
        )

    # Integridad de alias: un alias que sea el nombre canónico de OTRA ficha viva desvía la
    # resolución por nombre hacia la superviviente (p. ej. «Carrera de Ingeniería Electrónica» como
    # alias de la universidad mientras la carrera existe como ficha propia).
    drop_colliding_aliases(conn, user_id, identity_ids=[survivor_id])
    _log.info("identidades.merge.done", survivor=survivor_id, absorbed=absorbed_id)
    return True


def drop_colliding_aliases(
    conn: Connection, user_id: int, *, identity_ids: list[int] | None = None
) -> list[tuple[int, str, int]]:
    """Regla general: un alias NO puede ser el nombre canónico (normalizado) de otra identidad
    viva del mismo user — la ficha propia gana y el alias se quita. Si no, `resolve_target_name`
    y el resolve-or-create resolverían ese nombre a la dueña del alias y no a la ficha real
    (claims y afiliaciones desviados). Nace de fusiones viejas que dejaron en la universidad los
    nombres de sub-unidades que luego volvieron a existir. Con `identity_ids` acota a esas dueñas;
    sin él, barre todas. Devuelve `(dueña, alias, otra)` por cada alias quitado."""
    scope = "" if identity_ids is None else " AND i.id = ANY(:ids)"
    params: dict[str, object] = {"u": user_id}
    if identity_ids is not None:
        params["ids"] = list(identity_ids)
    rows = conn.execute(
        text(
            f"""
            SELECT i.id AS owner_id, a.alias, o.id AS other_id
            FROM mod_identidades i
            CROSS JOIN LATERAL unnest(i.aliases) AS a(alias)
            JOIN mod_identidades o
              ON o.user_id = i.user_id AND o.id <> i.id AND o.name_norm = memex_norm(a.alias)
            WHERE i.user_id = :u{scope}
            ORDER BY i.id, a.alias
            """
        ),
        params,
    ).all()
    dropped: list[tuple[int, str, int]] = []
    for owner_id, alias, other_id in rows:
        conn.execute(
            text(
                "UPDATE mod_identidades SET aliases = array_remove(aliases, :a), "
                "updated_at = NOW() WHERE id = :i AND user_id = :u"
            ),
            {"a": alias, "i": int(owner_id), "u": user_id},
        )
        dropped.append((int(owner_id), str(alias), int(other_id)))
        _log.info(
            "identidades.alias.collision_dropped",
            owner_id=int(owner_id),
            alias=str(alias),
            other_id=int(other_id),
        )
    return dropped


def split_identity(
    conn: Connection, user_id: int, survivor_id: int, name: str, *, kind: str | None = None
) -> int | None:
    """Deshace una fusión POR NOMBRE: vuelve a crear `name` como identidad propia a partir de lo que
    quedó dentro de `survivor_id` (la fusión no guarda la fila absorbida, pero sí su rastro: el
    nombre como alias, las menciones con `mentioned_name`, los identificadores movidos).

    1. crea la identidad (`kind` = el del superviviente salvo que se indique; `source='manual'`,
       `metadata.split_from`);
    2. quita el nombre de los alias del superviviente;
    3. re-apunta las menciones del superviviente cuyo `mentioned_name` normalizado es `name`;
    4. mueve los identificadores (email/handle) que coinciden con el email/handle de esas menciones
       y re-apunta también las menciones del remitente con esos emails.

    Lo que no se puede reconstruir (reclamos del libro, aristas, hijos) lo repone el flujo normal:
    re-procesar los correos crea los reclamos y el padre se deriva. Devuelve el id nuevo, o None si
    el superviviente no existe o el nombre está vacío."""
    n = (name or "").strip()
    if not n:
        return None
    surv = (
        conn.execute(
            text("SELECT kind FROM mod_identidades WHERE id = :s AND user_id = :u"),
            {"s": survivor_id, "u": user_id},
        )
        .mappings()
        .first()
    )
    if surv is None:
        return None
    new_id = int(
        conn.execute(
            text(
                """
                INSERT INTO mod_identidades (user_id, kind, display_name, source, metadata)
                VALUES (:u, :k, :n, 'manual', jsonb_build_object('split_from', :s))
                RETURNING id
                """
            ),
            {"u": user_id, "k": kind or str(surv["kind"]), "n": n, "s": survivor_id},
        ).scalar_one()
    )
    conn.execute(
        text(
            """
            UPDATE mod_identidades SET
              aliases = COALESCE((SELECT array_agg(x) FROM unnest(aliases) AS x
                                   WHERE memex_norm(x) <> memex_norm(:n)), '{}'),
              updated_at = NOW()
            WHERE id = :s AND user_id = :u
            """
        ),
        {"n": n, "s": survivor_id, "u": user_id},
    )
    moved = conn.execute(
        text(
            """
            UPDATE mod_identidades_mentions SET resolved_identity_id = :new
            WHERE user_id = :u AND resolved_identity_id = :s
              AND memex_norm(mentioned_name) = memex_norm(:n)
            RETURNING lower(COALESCE(email, '')) AS email, lower(COALESCE(handle, '')) AS handle
            """
        ),
        {"new": new_id, "u": user_id, "s": survivor_id, "n": n},
    ).mappings()
    vals = sorted({v for r in moved for v in (r["email"], r["handle"]) if v})
    if vals:
        conn.execute(
            text(
                "UPDATE mod_identidades_identifiers SET identity_id = :new "
                "WHERE user_id = :u AND identity_id = :s AND kind IN ('email','handle') "
                "AND lower(value) = ANY(:vals)"
            ),
            {"new": new_id, "u": user_id, "s": survivor_id, "vals": vals},
        )
        conn.execute(
            text(
                "UPDATE mod_identidades_mentions SET resolved_identity_id = :new "
                "WHERE user_id = :u AND resolved_identity_id = :s AND lower(email) = ANY(:vals)"
            ),
            {"new": new_id, "u": user_id, "s": survivor_id, "vals": vals},
        )
    _log.info(
        "identidades.split.done",
        survivor=survivor_id,
        new_id=new_id,
        name=n,
        identifiers=len(vals),
    )
    return new_id
