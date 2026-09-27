"""Ajustes del daemon (DB manda) y de los productores (defaults ⊕ fila), conversión de config."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Connection

from memex.notifications import settings as s

_DEFAULTS = {"hora": "07:00", "dias_tareas": 7, "lista": ["a", "b"], "flag": False, "factor": 1.5}


def test_daemon_settings_default_off_then_toggle(conn: Connection) -> None:
    st = s.get_settings(conn, 1, default_tz="America/Bogota")
    assert st.daemon_enabled is False and st.timezone == "America/Bogota"
    st = s.upsert_settings(conn, 1, daemon_enabled=True)
    assert st.daemon_enabled is True
    st = s.upsert_settings(conn, 1, timezone="Europe/Madrid")
    assert st.daemon_enabled is True and st.timezone == "Europe/Madrid"  # parcial
    assert s.get_settings(conn, 1).tz.key == "Europe/Madrid"
    with pytest.raises(Exception):  # noqa: B017 — ZoneInfoNotFoundError (KeyError en algunos builds)
        s.upsert_settings(conn, 1, timezone="Marte/Crater")


def test_producer_settings_defaults_merge_and_partial_upsert(conn: Connection) -> None:
    kw: dict[str, Any] = {
        "default_enabled": True,
        "default_render": "llm",
        "default_config": _DEFAULTS,
    }
    ps = s.get_producer_settings(conn, 1, "resumen_diario", **kw)
    assert ps.enabled and ps.render == "llm" and ps.config == _DEFAULTS and not ps.from_db
    ps = s.upsert_producer_settings(
        conn, 1, "resumen_diario", **kw, render="plantilla", config_changes={"hora": "06:30"}
    )
    assert ps.render == "plantilla" and ps.config["hora"] == "06:30" and ps.enabled is True
    ps = s.upsert_producer_settings(conn, 1, "resumen_diario", **kw, enabled=False)
    assert ps.enabled is False and ps.render == "plantilla" and ps.config["hora"] == "06:30"
    # Un default nuevo en código aparece aunque la fila sea vieja.
    wider = {**_DEFAULTS, "nueva": 3}
    ps = s.get_producer_settings(conn, 1, "resumen_diario", **{**kw, "default_config": wider})
    assert ps.config["nueva"] == 3 and ps.config["hora"] == "06:30" and ps.from_db
    with pytest.raises(ValueError):
        s.upsert_producer_settings(conn, 1, "resumen_diario", **kw, render="prosa")


def test_coerce_config_types_and_unknown_key() -> None:
    out = s.coerce_config(
        _DEFAULTS,
        {"hora": "08:15", "dias_tareas": "3", "lista": "x, y", "flag": "si", "factor": "2"},
    )
    assert out == {
        "hora": "08:15",
        "dias_tareas": 3,
        "lista": ["x", "y"],
        "flag": True,
        "factor": 2.0,
    }
    with pytest.raises(ValueError):
        s.coerce_config(_DEFAULTS, {"hora": "25h"})
    with pytest.raises(ValueError):
        s.coerce_config(_DEFAULTS, {"otra": "1"})
    with pytest.raises(ValueError):
        s.coerce_config(_DEFAULTS, {"dias_tareas": "tres"})
