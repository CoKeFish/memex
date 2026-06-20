"""Round-trip de los settings del resolvedor de identidades (`identidades_resolver_settings`)."""

from __future__ import annotations

import pytest
from sqlalchemy import Connection

from memex.modules.identidades.settings import ResolverSettings, get_settings, upsert_settings


def test_defaults_off_when_no_row(conn: Connection) -> None:
    s = get_settings(conn, user_id=1)
    assert s == ResolverSettings()
    assert s.resolver_enabled is False
    assert s.batch_maintenance_enabled is False
    assert s.min_confidence_merge == 0.75
    assert s.min_confidence_parent == 0.80
    assert s.max_calls_per_window == 16


def test_partial_upsert_keeps_other_fields(conn: Connection) -> None:
    upsert_settings(conn, 1, resolver_enabled=True)
    assert get_settings(conn, 1).resolver_enabled is True
    assert get_settings(conn, 1).batch_maintenance_enabled is False  # default conservado

    upsert_settings(conn, 1, min_confidence_merge=0.5)
    after = get_settings(conn, 1)
    assert after.resolver_enabled is True  # no se pisó con el upsert parcial
    assert after.min_confidence_merge == 0.5


def test_batch_flag_independent_of_resolver(conn: Connection) -> None:
    upsert_settings(conn, 1, batch_maintenance_enabled=True)
    s = get_settings(conn, 1)
    assert s.batch_maintenance_enabled is True
    assert s.resolver_enabled is False


def test_merge_confidence_out_of_range_raises(conn: Connection) -> None:
    with pytest.raises(ValueError, match="rango"):
        upsert_settings(conn, 1, min_confidence_merge=1.5)


def test_parent_confidence_out_of_range_raises(conn: Connection) -> None:
    with pytest.raises(ValueError, match="rango"):
        upsert_settings(conn, 1, min_confidence_parent=-0.1)


def test_max_calls_below_one_raises(conn: Connection) -> None:
    with pytest.raises(ValueError, match="mínimo 1"):
        upsert_settings(conn, 1, max_calls_per_window=0)
