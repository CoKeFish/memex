"""Seam de notificaciones: conformance del Protocol + el stub LoggingNotifier."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memex.notifications import LoggingNotifier, Notification, Notifier, build_notifier


def _sample() -> Notification:
    return Notification(
        kind="transport.leave_by",
        severity="alta",
        title="Salí ya",
        body="Salí antes de las 14:40 o no llegás a la reunión.",
        dedup_key="transport:1:42:leave_now",
        created_at=datetime(2026, 6, 17, 14, 10, tzinfo=UTC),
        payload={"event_id": 42, "verdict": "leave_now"},
    )


def test_logging_notifier_satisfies_protocol() -> None:
    assert isinstance(LoggingNotifier(), Notifier)


def test_build_notifier_returns_a_notifier() -> None:
    assert isinstance(build_notifier(), Notifier)


@pytest.mark.asyncio
async def test_logging_notifier_emits_without_error() -> None:
    await LoggingNotifier().notify(_sample())  # el stub solo loguea: ni persiste ni lanza
