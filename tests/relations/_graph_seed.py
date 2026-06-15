"""Helpers de SEED para los tests del grafo (co-ocurrencia / weaves / mantenimiento).

Insertan filas crudas en las tablas `mod_*` (sin pasar por el camino de escritura de cada módulo):
los tests del grafo seedean el estado y luego ejercen la función bajo prueba
(`generate_cooccurrence`, `weave_*`, `reconcile_graph`). No son fixtures: se importan directo.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection


def run(sql: str, **params: Any) -> Any:
    """Ejecuta `sql` en su propia tx (commitea al salir); devuelve el scalar si la query retorna."""
    with connection() as c:
        result = c.execute(text(sql), params)
        return result.scalar() if result.returns_rows else None


def finance(merchant: str, inbox_ids: list[int], identity_id: int | None = None) -> int:
    """Transacción cruda + su consolidado + el link. El VÉRTICE es el consolidado (devuelto); su
    procedencia de inbox es transitiva (link → crudo.source_inbox_ids). `identity_id` setea el
    `counterparty_identity_id` del consolidado (para la arista de contraparte)."""
    crudo = int(
        run(
            "INSERT INTO mod_finance_transactions "
            "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, counterparty) "
            "VALUES (1, :ids, 'egreso', 100, 'COP', NOW(), :m) RETURNING id",
            ids=inbox_ids,
            m=merchant,
        )
    )
    cons = int(
        run(
            "INSERT INTO mod_finance_consolidated (user_id, direction, amount, currency, "
            "occurred_at, counterparty, counterparty_identity_id) "
            "VALUES (1, 'egreso', 100, 'COP', NOW(), :m, :iid) RETURNING id",
            m=merchant,
            iid=identity_id,
        )
    )
    run(
        "INSERT INTO mod_finance_transaction_links (user_id, consolidated_id, transaction_id) "
        "VALUES (1, :c, :t)",
        c=cons,
        t=crudo,
    )
    return cons


def hack(name: str, inbox_ids: list[int]) -> int:
    return int(
        run(
            "INSERT INTO mod_hackathones_events (user_id, source_inbox_ids, name) "
            "VALUES (1, :ids, :n) RETURNING id",
            ids=inbox_ids,
            n=name,
        )
    )


def person(name: str) -> int:
    return int(
        run(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'persona', :n) RETURNING id",
            n=name,
        )
    )


def org(name: str) -> int:
    return int(
        run(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'organizacion', :n) RETURNING id",
            n=name,
        )
    )


def producto(name: str) -> int:
    return int(
        run(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'producto', :n) RETURNING id",
            n=name,
        )
    )


def link_person_org(person_id: int, org_id: int) -> None:
    run(
        "INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id) VALUES (1, :p, :o)",
        p=person_id,
        o=org_id,
    )


def mention(identity_id: int, inbox_ids: list[int], kind: str = "persona") -> None:
    # `kind` es cosmético: el slug del vértice sale de `mod_identidades.kind` (no de la mención);
    # lo que importa es a qué identidad apunta `resolved_identity_id`.
    run(
        "INSERT INTO mod_identidades_mentions "
        "(user_id, source_inbox_ids, mentioned_name, resolved_kind, resolved_identity_id) "
        "VALUES (1, :ids, 'X', :k, :p)",
        ids=inbox_ids,
        k=kind,
        p=identity_id,
    )


def calendar(title: str, inbox_ids: list[int]) -> int:
    """Evento crudo + su consolidado + el link. El VÉRTICE es el consolidado (devuelto)."""
    crudo = int(
        run(
            "INSERT INTO mod_calendar_events (user_id, source_inbox_ids, title, starts_on) "
            "VALUES (1, :ids, :t, DATE '2026-07-01') RETURNING id",
            ids=inbox_ids,
            t=title,
        )
    )
    cons = int(
        run(
            "INSERT INTO mod_calendar_consolidated (user_id, title, starts_on) "
            "VALUES (1, :t, DATE '2026-07-01') RETURNING id",
            t=title,
        )
    )
    run(
        "INSERT INTO mod_calendar_event_links (user_id, consolidated_id, event_id) "
        "VALUES (1, :c, :e)",
        c=cons,
        e=crudo,
    )
    return cons


def set_parent(child: int, parent: int | None) -> None:
    run("UPDATE mod_identidades SET parent_identity_id = :p WHERE id = :c", p=parent, c=child)


def pair(e: Any) -> set[tuple[str, int]]:
    """El par no orientado `{(slug, id), (slug, id)}` de una arista, para comparar sin dirección."""
    return {(e.src.slug, e.src.id), (e.dst.slug, e.dst.id)}
