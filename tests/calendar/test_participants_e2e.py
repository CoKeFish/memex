"""Integración E2E de organizador/asistentes → identidad: el camino REAL completo.

sync (`run_pull`, proveedor FALSO solo en el borde de red) → persistencia de participantes →
consolidación (`run_consolidation`, que teje en su finisher) → aristas del grafo, con la perilla
`asiste_includes_declined` y la reconciliación al des-invitar. Todo lo de adentro es código de
producción (no se mockea ni el weave ni el reconcile ni el settings writer).
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.calendar.consolidate import run_consolidation
from memex.modules.calendar.providers.base import (
    CalendarParticipant,
    ProviderEvent,
    ProviderPage,
)
from memex.modules.calendar.settings import set_asiste_includes_declined
from memex.modules.calendar.sync import run_pull
from memex.relations.edges import list_edges
from memex.relations.maintenance import reconcile_graph
from tests.calendar.test_sync_upsert import FakeProvider
from tests.relations._graph_seed import desconocido, email_identifier, person, producto


def _account() -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_provider_accounts "
                    "(user_id, provider, account_label, calendar_id, token_path_env) "
                    "VALUES (1, 'google', 'me@gmail.com', 'primary', 'CAL_TOKEN_PATH') RETURNING id"
                )
            ).scalar_one()
        )


def _p(
    email: str,
    *,
    status: str | None = None,
    is_self: bool = False,
    is_resource: bool = False,
) -> CalendarParticipant:
    return CalendarParticipant(
        email=email, response_status=status, is_self=is_self, is_resource=is_resource
    )


def _event(
    pid: str,
    *,
    etag: str,
    organizer: CalendarParticipant | None = None,
    attendees: tuple[CalendarParticipant, ...] = (),
) -> ProviderEvent:
    return ProviderEvent(
        provider_event_id=pid,
        title="Reunión",
        starts_on=date(2026, 7, 1),
        etag=etag,
        organizer=organizer,
        attendees=attendees,
    )


async def _pull(aid: int, ev: ProviderEvent, token: str) -> None:
    await run_pull(1, aid, client=FakeProvider(ProviderPage(events=(ev,), next_sync_token=token)))


def _triples() -> set[tuple[str, str, int]]:
    with connection() as c:
        edges = list_edges(c, 1, producer="calendar")
    return {(e.relation_type, e.dst.slug, e.dst.id) for e in edges}


@pytest.mark.asyncio
async def test_e2e_sync_consolidate_weaves_participant_edges() -> None:
    # Directorio resuelto por email; self/resource/producto TIENEN identidad a propósito, para
    # probar que los filtra el FLAG (no la falta de identidad).
    ana, beto, carlos = person("Ana"), person("Beto"), person("Carlos")
    desc = desconocido("alguien")
    prod = producto("Steam")
    yo, sala = person("Yo"), person("Sala A")
    for ident, mail in [
        (ana, "ana@example.com"),
        (beto, "beto@example.com"),
        (carlos, "carlos@example.com"),
        (desc, "x@raro.com"),
        (prod, "steam@valvesoftware.com"),
        (yo, "yo@example.com"),
        (sala, "sala@x.com"),
    ]:
        email_identifier(ident, mail)

    aid = _account()
    ev = _event(
        "ev1",
        etag="e1",
        organizer=_p("ana@example.com"),
        attendees=(
            _p("beto@example.com", status="accepted"),
            _p("carlos@example.com", status="declined"),  # rechazó → fuera (perilla off)
            _p("x@raro.com", status="accepted"),  # desconocido → enlaza
            _p("steam@valvesoftware.com", status="accepted"),  # producto → vetado
            _p("yo@example.com", status="accepted", is_self=True),  # self → fuera
            _p("sala@x.com", status="accepted", is_resource=True),  # recurso → fuera
        ),
    )
    await _pull(aid, ev, "T1")
    run_consolidation(1)

    assert _triples() == {
        ("organiza", "identidades:person", ana),
        ("asiste", "identidades:person", beto),
        ("asiste", "identidades:desconocido", desc),
    }
    # todas nacen del evento CONSOLIDADO (no del raw)
    with connection() as c:
        cid = int(
            c.execute(
                text("SELECT id FROM mod_calendar_consolidated WHERE user_id = 1")
            ).scalar_one()
        )
        assert all(
            (e.src.slug, e.src.id) == ("calendar", cid)
            for e in list_edges(c, 1, producer="calendar")
        )


@pytest.mark.asyncio
async def test_e2e_declined_toggle_then_reconcile() -> None:
    ana, carlos = person("Ana"), person("Carlos")
    email_identifier(ana, "ana@example.com")
    email_identifier(carlos, "carlos@example.com")
    aid = _account()
    ev = _event(
        "ev1",
        etag="e1",
        organizer=_p("ana@example.com"),
        attendees=(_p("carlos@example.com", status="declined"),),
    )
    await _pull(aid, ev, "T1")
    run_consolidation(1)
    assert _triples() == {("organiza", "identidades:person", ana)}  # declined fuera por default

    # Prender la perilla y re-consolidar: el finisher re-teje y aparece el «asiste» del declined.
    with connection() as c:
        set_asiste_includes_declined(c, 1, True)
    run_consolidation(1)
    assert _triples() == {
        ("organiza", "identidades:person", ana),
        ("asiste", "identidades:person", carlos),
    }

    # Apagar la perilla: el weave es ADITIVO (no quita); el reconcile sí poda el stale.
    with connection() as c:
        set_asiste_includes_declined(c, 1, False)
    run_consolidation(1)
    assert ("asiste", "identidades:person", carlos) in _triples()  # sigue (weave no borra)
    with connection() as c:
        reconcile_graph(c, 1)
    assert _triples() == {("organiza", "identidades:person", ana)}  # reconcile lo quitó


@pytest.mark.asyncio
async def test_e2e_uninvite_resync_reconcile_prunes_asiste() -> None:
    ana, beto = person("Ana"), person("Beto")
    email_identifier(ana, "ana@example.com")
    email_identifier(beto, "beto@example.com")
    aid = _account()
    ev1 = _event(
        "ev1",
        etag="e1",
        organizer=_p("ana@example.com"),
        attendees=(_p("beto@example.com", status="accepted"),),
    )
    await _pull(aid, ev1, "T1")
    run_consolidation(1)
    assert ("asiste", "identidades:person", beto) in _triples()

    # Re-sync con etag nuevo: a beto lo des-invitan (ya no está en attendees).
    ev2 = _event("ev1", etag="e2", organizer=_p("ana@example.com"))
    await _pull(aid, ev2, "T2")
    run_consolidation(1)
    with connection() as c:
        reconcile_graph(c, 1)
    assert _triples() == {("organiza", "identidades:person", ana)}  # el «asiste» de beto se podó
