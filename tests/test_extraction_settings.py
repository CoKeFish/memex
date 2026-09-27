"""Round-trip de los settings de extracción (`extraction_settings`): default ON + toggle."""

from __future__ import annotations

from sqlalchemy import Connection

from memex.modules.extraction_settings import (
    ExtractionSettings,
    get_extraction_settings,
    upsert_extraction_settings,
)


def test_default_routing_on_when_no_row(conn: Connection) -> None:
    s = get_extraction_settings(conn, user_id=1)
    assert s == ExtractionSettings()
    assert s.routing_enabled is True  # sin fila → ruteo encendido (comportamiento previo)


def test_upsert_toggles_routing(conn: Connection) -> None:
    s = upsert_extraction_settings(conn, 1, routing_enabled=False)
    assert s.routing_enabled is False
    assert get_extraction_settings(conn, 1).routing_enabled is False

    s2 = upsert_extraction_settings(conn, 1, routing_enabled=True)
    assert s2.routing_enabled is True
    assert get_extraction_settings(conn, 1).routing_enabled is True


def test_partial_upsert_preserves(conn: Connection) -> None:
    upsert_extraction_settings(conn, 1, routing_enabled=False)
    s = upsert_extraction_settings(conn, 1)  # sin campos → preserva lo actual
    assert s.routing_enabled is False
