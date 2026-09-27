"""El Notifier real: conformance, cableado de build_notifier y persistencia vía notify()."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memex.db import connection
from memex.notifications import (
    Notification,
    Notifier,
    PersistentNotifier,
    build_notifier,
    store,
)


def test_persistent_notifier_satisfies_protocol() -> None:
    assert isinstance(PersistentNotifier(), Notifier)


def test_build_notifier_returns_persistent() -> None:
    assert isinstance(build_notifier(), PersistentNotifier)


@pytest.mark.asyncio
async def test_notify_persists_and_collapses() -> None:
    n = Notification(
        kind="transport.leave_by",
        severity="alta",
        title="Salí ya",
        body="cuerpo",
        dedup_key="transport:1:7:leave_now",
        created_at=datetime(2026, 6, 17, 14, 10, tzinfo=UTC),
        user_id=1,
        payload={"event_id": 7},
        deep_link="/calendario",
    )
    await PersistentNotifier().notify(n)
    await PersistentNotifier().notify(n)  # idempotente: colapsa por (user_id, dedup_key)
    with connection() as c:
        rows = store.list_active(c, user_id=1, limit=50)
    assert len(rows) == 1
    assert rows[0]["kind"] == "transport.leave_by"
    assert rows[0]["deep_link"] == "/calendario"
