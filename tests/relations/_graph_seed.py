"""Helpers de SEED para los tests del grafo (co-ocurrencia / weaves / mantenimiento).

Insertan filas crudas en las tablas `mod_*` (sin pasar por el camino de escritura de cada módulo):
los tests del grafo seedean el estado y luego ejercen la función bajo prueba
(`generate_cooccurrence`, `weave_*`, `reconcile_graph`). No son fixtures: se importan directo.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.normalize import norm_identifier


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


def desconocido(name: str) -> int:
    return int(
        run(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'desconocido', :n) RETURNING id",
            n=name,
        )
    )


def email_identifier(identity_id: int, email: str) -> None:
    """Identifier de email de una identidad. `value_norm` con el MISMO `norm_identifier` que usa el
    sync al persistir participantes → el join del tejedor (`email_norm = value_norm`) matchea."""
    run(
        "INSERT INTO mod_identidades_identifiers "
        "(user_id, identity_id, platform, kind, value, value_norm) "
        "VALUES (1, :iid, '', 'email', :v, :vn)",
        iid=identity_id,
        v=email,
        vn=norm_identifier("email", email),
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


def calendar_event(title: str, inbox_ids: list[int] | None = None) -> tuple[int, int]:
    """Evento crudo + su consolidado + el link. Devuelve `(consolidado, event_id_crudo)`: el
    consolidado es el VÉRTICE; el crudo es donde cuelgan los participantes (`calendar_participant`).
    `inbox_ids` opcional (los participantes no dependen de la co-ocurrencia)."""
    crudo = int(
        run(
            "INSERT INTO mod_calendar_events (user_id, source_inbox_ids, title, starts_on) "
            "VALUES (1, :ids, :t, DATE '2026-07-01') RETURNING id",
            ids=inbox_ids if inbox_ids is not None else [],
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
    return cons, crudo


def calendar(title: str, inbox_ids: list[int]) -> int:
    """Evento consolidado (el VÉRTICE), sin participantes — para co-ocurrencia / huérfanas."""
    return calendar_event(title, inbox_ids)[0]


def calendar_participant(
    event_id: int,
    role: str,
    email: str,
    *,
    is_self: bool = False,
    is_resource: bool = False,
    response_status: str | None = None,
) -> None:
    """Cuelga un participante del evento CRUDO (`event_id` de `calendar_event`). `email_norm` con el
    mismo `norm_identifier` que el sync, para que el join del tejedor con identifiers matchee."""
    run(
        "INSERT INTO mod_calendar_event_participants "
        "(user_id, event_id, role, display_name, email, email_norm, "
        " is_self, is_resource, response_status) "
        "VALUES (1, :e, :r, '', :em, :en, :slf, :res, :rs)",
        e=event_id,
        r=role,
        em=email,
        en=norm_identifier("email", email) if email else "",
        slf=is_self,
        res=is_resource,
        rs=response_status,
    )


def calendar_declined_setting(value: bool) -> None:
    """Setea la perilla `asiste_includes_declined` (module_settings.config del módulo calendar): ¿un
    invitado `declined` recibe «asiste»? Mismo upsert que el writer real de `calendar.settings`."""
    run(
        "INSERT INTO module_settings (user_id, module_slug, config) "
        "VALUES (1, 'calendar', "
        "jsonb_build_object('asiste_includes_declined', CAST(:v AS boolean))) "
        "ON CONFLICT (user_id, module_slug) "
        "DO UPDATE SET config = module_settings.config || EXCLUDED.config",
        v=value,
    )


def set_edge_verdict(producer: str, verdict: str) -> None:
    """Fija el `verdict` de todas las aristas de un producer (simula una decisión humana)."""
    run("UPDATE relation_edges SET verdict = :v WHERE producer = :p", v=verdict, p=producer)


def set_parent(child: int, parent: int | None) -> None:
    run("UPDATE mod_identidades SET parent_identity_id = :p WHERE id = :c", p=parent, c=child)


def pair(e: Any) -> set[tuple[str, int]]:
    """El par no orientado `{(slug, id), (slug, id)}` de una arista, para comparar sin dirección."""
    return {(e.src.slug, e.src.id), (e.dst.slug, e.dst.id)}
