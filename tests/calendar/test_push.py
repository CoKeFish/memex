"""Write-back (egress) con un proveedor GRABADOR (sin red).

Cubre: crea lo que falta en la cuenta, NO duplica lo que el usuario ya tiene ahí, actualiza al
cambiar el contenido, borra los tombstones, respeta `write_back`, y —el test GATE— NO entra en
loop cuando el evento que memex creó vuelve por el pull (echo-suppression por firma de contenido).
Y la POLÍTICA por cuenta (0099): solo salen los estados permitidos y lo que no terminó; lo que
deja de cumplirla se borra del proveedor.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import ClassVar

import pytest
from sqlalchemy import text

from memex.core.source import HealthResult
from memex.db import connection
from memex.modules.calendar.consolidate import run_consolidation
from memex.modules.calendar.providers.base import (
    ProviderEventRef,
    ProviderEventWrite,
    ProviderPage,
)
from memex.modules.calendar.sync import run_push


class RecordingProvider:
    """Registra las llamadas de escritura; create devuelve ids/etags incrementales."""

    name: ClassVar[str] = "google"

    def __init__(self) -> None:
        self.created: list[ProviderEventWrite] = []
        self.updated: list[tuple[str, ProviderEventWrite]] = []
        self.deleted: list[str] = []
        self._n = 0

    async def health_check(self) -> HealthResult:
        return HealthResult(status="healthy", detail="fake", checked_at=datetime.now(UTC))

    async def list_delta(
        self, *, sync_token: str | None = None, page_token: str | None = None
    ) -> ProviderPage:
        return ProviderPage()

    async def create_event(self, ev: ProviderEventWrite) -> ProviderEventRef:
        self.created.append(ev)
        self._n += 1
        return ProviderEventRef(provider_event_id=f"prov-{self._n}", etag=f"etag-{self._n}")

    async def update_event(
        self, *, provider_event_id: str, etag: str | None, ev: ProviderEventWrite
    ) -> ProviderEventRef:
        self.updated.append((provider_event_id, ev))
        return ProviderEventRef(
            provider_event_id=provider_event_id, etag=f"etag-upd-{len(self.updated)}"
        )

    async def delete_event(self, *, provider_event_id: str, etag: str | None) -> None:
        self.deleted.append(provider_event_id)


def _seed_account(
    *,
    write_back: bool = True,
    push_commitments: str = "{confirmado,provisional,anuncio}",
    past_days: int | None = None,
) -> int:
    """Cuenta write_back. Por default la política es PERMISIVA (todo estado, sin piso) para que los
    tests de mecánica no dependan de ella; los de política la fijan explícitamente."""
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_calendar_provider_accounts
                      (user_id, provider, account_label, calendar_id, token_path_env, write_back,
                       push_commitments, push_past_days)
                    VALUES (1, 'google', 'me@gmail.com', 'primary', 'X', :wb,
                            CAST(:pc AS TEXT[]), :pd)
                    RETURNING id
                    """
                ),
                {"wb": write_back, "pc": push_commitments, "pd": past_days},
            ).scalar_one()
        )


def _seed_cons(
    title: str = "Cita",
    *,
    start_time: time | None = time(15, 0),
    starts_on: date = date(2026, 6, 3),
    commitment: str = "anuncio",
    recurrence: list[str] | None = None,
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_consolidated "
                    "(user_id, title, starts_on, start_time, commitment, recurrence) "
                    "VALUES (1, :t, :d, :st, :cm, CAST(:rec AS TEXT[])) RETURNING id"
                ),
                {
                    "t": title,
                    "d": starts_on,
                    "st": start_time,
                    "cm": commitment,
                    "rec": recurrence or [],
                },
            ).scalar_one()
        )


def _seed_event_linked(cons_id: int, account_id: int, *, marker: int | None) -> int:
    """Un evento de proveedor (de `account_id`) linkeado a `cons_id`. `marker` set ⇒ eco de memex,
    None ⇒ evento PROPIO del usuario."""
    meta = f'{{"memex_consolidated_id": {marker}}}' if marker is not None else "{}"
    peid = f"echo-{marker}" if marker is not None else "user-evt"
    with connection() as c:
        eid = int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_calendar_events
                      (user_id, source_inbox_ids, title, starts_on, origin, provider,
                       provider_account_id, provider_event_id, metadata)
                    VALUES (1, ARRAY[]::bigint[], 'Cita', :d, 'provider', 'google', :aid, :peid,
                            CAST(:meta AS JSONB))
                    RETURNING id
                    """
                ),
                {"d": date(2026, 6, 3), "aid": account_id, "peid": peid, "meta": meta},
            ).scalar_one()
        )
        c.execute(
            text(
                "INSERT INTO mod_calendar_event_links (user_id, consolidated_id, event_id) "
                "VALUES (1, :cid, :eid)"
            ),
            {"cid": cons_id, "eid": eid},
        )
    return eid


def _wb(cons_id: int, account_id: int) -> dict[str, object] | None:
    with connection() as c:
        row = c.execute(
            text(
                "SELECT provider_event_id, state, last_pushed_signature "
                "FROM mod_calendar_writeback "
                "WHERE consolidated_id = :c AND provider_account_id = :a"
            ),
            {"c": cons_id, "a": account_id},
        ).first()
    return None if row is None else {"peid": row[0], "state": row[1], "sig": row[2]}


@pytest.mark.asyncio
async def test_push_creates_missing_event() -> None:
    aid = _seed_account()
    cons = _seed_cons()
    fake = RecordingProvider()

    stats = await run_push(1, aid, client=fake)

    assert stats.created == 1
    assert len(fake.created) == 1
    assert fake.created[0].memex_consolidated_id == str(cons)
    wb = _wb(cons, aid)
    assert wb is not None and wb["state"] == "synced" and wb["peid"] == "prov-1"


@pytest.mark.asyncio
async def test_push_idempotent_when_unchanged() -> None:
    aid = _seed_account()
    _seed_cons()
    fake = RecordingProvider()

    await run_push(1, aid, client=fake)
    stats2 = await run_push(1, aid, client=fake)  # firma sin cambios → skip

    assert stats2.skipped == 1
    assert stats2.created == 0
    assert len(fake.created) == 1  # no re-creó


@pytest.mark.asyncio
async def test_push_updates_on_content_change() -> None:
    aid = _seed_account()
    cons = _seed_cons(title="Cita")
    fake = RecordingProvider()
    await run_push(1, aid, client=fake)

    with connection() as c:
        c.execute(
            text("UPDATE mod_calendar_consolidated SET title = 'Cita movida' WHERE id = :id"),
            {"id": cons},
        )
    stats = await run_push(1, aid, client=fake)

    assert stats.updated == 1
    assert len(fake.updated) == 1
    assert fake.updated[0][0] == "prov-1"  # actualizó el evento que había creado


@pytest.mark.asyncio
async def test_push_skips_when_account_has_user_event() -> None:
    aid = _seed_account()
    cons = _seed_cons()
    _seed_event_linked(cons, aid, marker=None)  # el usuario YA tiene este evento en la cuenta
    fake = RecordingProvider()

    stats = await run_push(1, aid, client=fake)

    assert stats.skipped == 1
    assert len(fake.created) == 0  # no duplica lo que el user puso


@pytest.mark.asyncio
async def test_push_deletes_tombstoned_consolidated() -> None:
    aid = _seed_account()
    cons = _seed_cons()
    fake = RecordingProvider()
    await run_push(1, aid, client=fake)  # crea prov-1

    with connection() as c:
        c.execute(
            text(
                "UPDATE mod_calendar_consolidated SET deleted = TRUE, deleted_source = 'user' "
                "WHERE id = :id"
            ),
            {"id": cons},
        )
    stats = await run_push(1, aid, client=fake)

    assert stats.deleted == 1
    assert fake.deleted == ["prov-1"]
    assert _wb(cons, aid)["state"] == "deleted"  # type: ignore[index]


@pytest.mark.asyncio
async def test_push_noop_when_not_write_back() -> None:
    aid = _seed_account(write_back=False)
    _seed_cons()
    fake = RecordingProvider()

    stats = await run_push(1, aid, client=fake)

    assert stats.created == 0
    assert len(fake.created) == 0


@pytest.mark.asyncio
async def test_push_then_pulled_echo_does_not_loop() -> None:
    """GATE de loop-avoidance: lo que memex crea, al volver por el pull (eco), NO se re-escribe."""
    aid = _seed_account()
    cons = _seed_cons(title="Cita")
    fake = RecordingProvider()

    await run_push(1, aid, client=fake)  # crea prov-1 en la cuenta
    assert len(fake.created) == 1

    # Simula el pull que reimporta el evento que memex creó (eco con marcador → su consolidado).
    _seed_event_linked(cons, aid, marker=cons)
    run_consolidation(1)  # linkea el eco; el ganador/firma del consolidado NO cambian

    stats = await run_push(1, aid, client=fake)  # debe NO crear ni actualizar

    assert stats.skipped == 1
    assert len(fake.created) == 1  # sigue en 1
    assert len(fake.updated) == 0  # no hubo update espurio


# ---- política por cuenta (0099) ------------------------------------------------------------------

TODAY = date(2026, 9, 11)


@pytest.mark.asyncio
async def test_push_policy_only_allowed_commitments() -> None:
    """Default de una cuenta nueva: solo `confirmado`. Un anuncio no sale (cuenta en filtered)."""
    aid = _seed_account(push_commitments="{confirmado}")
    _seed_cons("Anuncio", commitment="anuncio")
    _seed_cons("Provisional", commitment="provisional")
    _seed_cons("Clase", commitment="confirmado")
    fake = RecordingProvider()

    stats = await run_push(1, aid, client=fake, today=TODAY)

    assert stats.created == 1 and stats.filtered == 2
    assert [e.title for e in fake.created] == ["Clase"]


@pytest.mark.asyncio
async def test_push_policy_floor_skips_finished_events_and_keeps_live_series() -> None:
    """Piso `hoy - 0`: lo que ya terminó no sale; una serie cuya última ocurrencia es futura sí, y
    una serie sin fin siempre."""
    aid = _seed_account(push_commitments="{confirmado}", past_days=0)
    _seed_cons("Vieja", commitment="confirmado", starts_on=date(2026, 6, 3))
    _seed_cons("Hoy", commitment="confirmado", starts_on=TODAY)
    _seed_cons(
        "Clase",
        commitment="confirmado",
        starts_on=date(2026, 9, 2),
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;UNTIL=20260925"],
    )
    _seed_cons(
        "Vencida",
        commitment="confirmado",
        starts_on=date(2026, 5, 4),
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20260601"],
    )
    _seed_cons(
        "Sin fin",
        commitment="confirmado",
        starts_on=date(2026, 1, 5),
        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
    )
    fake = RecordingProvider()

    stats = await run_push(1, aid, client=fake, today=TODAY)

    assert sorted(e.title for e in fake.created) == ["Clase", "Hoy", "Sin fin"]
    assert stats.created == 3 and stats.filtered == 2


@pytest.mark.asyncio
async def test_push_policy_past_days_window_and_no_floor() -> None:
    aid7 = _seed_account(push_commitments="{confirmado}", past_days=7)
    cons = _seed_cons("Reciente", commitment="confirmado", starts_on=TODAY - timedelta(days=5))
    fake = RecordingProvider()
    assert (await run_push(1, aid7, client=fake, today=TODAY)).created == 1

    with connection() as c:
        c.execute(
            text("UPDATE mod_calendar_consolidated SET starts_on = :d WHERE id = :id"),
            {"d": TODAY - timedelta(days=30), "id": cons},
        )
    stats = await run_push(1, aid7, client=fake, today=TODAY)
    assert stats.deleted == 1 and fake.deleted == ["prov-1"]  # venció el piso → se retira

    with connection() as c:  # sin piso: todo el histórico sale
        c.execute(
            text("UPDATE mod_calendar_provider_accounts SET push_past_days = NULL WHERE id = :id"),
            {"id": aid7},
        )
    stats = await run_push(1, aid7, client=fake, today=TODAY)
    assert stats.created == 1 and len(fake.created) == 2


@pytest.mark.asyncio
async def test_push_removes_event_that_left_policy() -> None:
    """Ya escrito y degradado a provisional → se borra del proveedor (misma vía que tombstone)."""
    aid = _seed_account(push_commitments="{confirmado}")
    cons = _seed_cons("Clase", commitment="confirmado")
    fake = RecordingProvider()
    await run_push(1, aid, client=fake, today=TODAY)  # crea prov-1

    with connection() as c:
        c.execute(
            text("UPDATE mod_calendar_consolidated SET commitment = 'provisional' WHERE id = :id"),
            {"id": cons},
        )
    stats = await run_push(1, aid, client=fake, today=TODAY)

    assert stats.deleted == 1 and fake.deleted == ["prov-1"]
    assert _wb(cons, aid)["state"] == "deleted"  # type: ignore[index]
    # Vuelve a confirmado → se vuelve a crear (nuevo id en el proveedor).
    with connection() as c:
        c.execute(
            text("UPDATE mod_calendar_consolidated SET commitment = 'confirmado' WHERE id = :id"),
            {"id": cons},
        )
    stats = await run_push(1, aid, client=fake, today=TODAY)
    assert stats.created == 1 and _wb(cons, aid)["peid"] == "prov-2"  # type: ignore[index]


@pytest.mark.asyncio
async def test_push_signature_includes_recurrence() -> None:
    """Cambiar la regla de la serie es un cambio de contenido → update en el proveedor."""
    aid = _seed_account()
    cons = _seed_cons("Clase", recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20260925"])
    fake = RecordingProvider()
    await run_push(1, aid, client=fake)
    assert fake.created[0].recurrence == ("RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20260925",)

    with connection() as c:
        c.execute(
            text(
                "UPDATE mod_calendar_consolidated SET recurrence = "
                "ARRAY['RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20260925'] WHERE id = :id"
            ),
            {"id": cons},
        )
    stats = await run_push(1, aid, client=fake)

    assert stats.updated == 1
    assert fake.updated[0][1].recurrence == ("RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20260925",)


@pytest.mark.asyncio
async def test_push_treats_stale_echo_as_users_event() -> None:
    """Un evento de la cuenta con marca de OTRO consolidado (eco de una base anterior) ya está en el
    calendario del usuario: no se crea otra copia."""
    aid = _seed_account()
    cons = _seed_cons("Cumpleaños")
    _seed_event_linked(cons, aid, marker=cons + 1000)  # marca vieja, de otro id
    fake = RecordingProvider()

    stats = await run_push(1, aid, client=fake)

    assert stats.skipped == 1 and len(fake.created) == 0


@pytest.mark.asyncio
async def test_push_retracts_its_copy_when_user_already_has_the_event() -> None:
    """memex creó su copia; después aparece (por el pull) el evento propio del usuario para el mismo
    consolidado → memex retira la suya en vez de dejar dos."""
    aid = _seed_account()
    cons = _seed_cons("Cita")
    fake = RecordingProvider()
    await run_push(1, aid, client=fake)  # crea prov-1
    _seed_event_linked(cons, aid, marker=None)  # el usuario ya lo tenía en su calendario

    stats = await run_push(1, aid, client=fake)

    assert stats.deleted == 1 and fake.deleted == ["prov-1"]
    assert _wb(cons, aid)["state"] == "deleted"  # type: ignore[index]
    assert (await run_push(1, aid, client=fake)).skipped == 1  # y no vuelve a crear
