"""Fechas del plugin outlook-desktop: el reloj COM es hora LOCAL etiquetada como UTC.

Evidencia real (2026-09-06, PC en America/Bogota): `item.ReceivedTime` =
`pywintypes.datetime(2026, 9, 5, 4, 9, 37, tzinfo=TimeZoneInfo('GMT Standard Time', True))`
mientras la cabecera `Date:` del mismo correo decía `Sat, 05 Sep 2026 09:09:12 +0000`. Tomar el
valor tal cual guardaba 04:09Z (5 h antes de la realidad).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "outlook-desktop" / "__init__.py"
BOGOTA = ZoneInfo("America/Bogota")


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("outlook_desktop_plugin_test", PLUGIN)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m  # pydantic/dataclasses resuelven el namespace por sys.modules
    spec.loader.exec_module(m)
    return m


class _Item:
    def __init__(self, received: object) -> None:
        self.ReceivedTime = received


def test_com_wallclock_etiquetado_utc_se_interpreta_como_local(mod: ModuleType) -> None:
    com_value = datetime(2026, 9, 5, 4, 9, 37, 872000, tzinfo=UTC)  # lo que entrega pywin32
    got = mod.com_wallclock_to_utc(com_value, local_tz=BOGOTA)
    assert got == datetime(2026, 9, 5, 9, 9, 37, 872000, tzinfo=UTC)
    # naive → misma interpretación (reloj local)
    assert mod.com_wallclock_to_utc(datetime(2026, 9, 5, 4, 9, 37), local_tz=BOGOTA) == datetime(
        2026, 9, 5, 9, 9, 37, tzinfo=UTC
    )
    # ISO en texto también; basura → None
    assert mod.com_wallclock_to_utc("2026-05-04T17:07:11", local_tz=BOGOTA) == datetime(
        2026, 5, 4, 22, 7, 11, tzinfo=UTC
    )
    assert mod.com_wallclock_to_utc("no es fecha", local_tz=BOGOTA) is None
    assert mod.com_wallclock_to_utc(None, local_tz=BOGOTA) is None


def test_received_dt_usa_la_zona_local_y_cae_a_ahora(mod: ModuleType) -> None:
    item = _Item(datetime(2026, 5, 4, 17, 7, 11, 636000, tzinfo=UTC))
    assert mod._received_dt(item, local_tz=BOGOTA) == datetime(
        2026, 5, 4, 22, 7, 11, 636000, tzinfo=UTC
    )
    before = datetime.now(UTC)
    fallback = mod._received_dt(_Item(None), local_tz=BOGOTA)
    assert fallback >= before and fallback.tzinfo is not None


def test_header_date_prefiere_la_cabecera_con_offset(mod: ModuleType) -> None:
    assert mod.header_date({"date": "Sat, 05 Sep 2026 09:09:12 +0000 (UTC)"}) == datetime(
        2026, 9, 5, 9, 9, 12, tzinfo=UTC
    )
    assert mod.header_date({"date": "Mon, 4 May 2026 17:07:11 -0500"}) == datetime(
        2026, 5, 4, 22, 7, 11, tzinfo=UTC
    )
    assert mod.header_date({"date": "cualquier cosa"}) is None
    assert mod.header_date({}) is None
    # fuera de rango razonable (relojes rotos) → None y se usa la recepción
    assert mod.header_date({"date": "Mon, 4 May 1998 17:07:11 -0500"}) is None
