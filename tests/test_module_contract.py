"""Conformancia de interfaz de los módulos de extracción (contrato `InterestModule`).

Modelo de interfaz (estilo C#): el contrato OBLIGA a que CADA módulo registrado IMPLEMENTE sus
miembros — `persist`, `dedup`, `read_for_inbox`, `health_check` — con el MISMO NOMBRE en todos; el
contrato NO dicta el cuerpo. La obligación se hace efectiva acá (+ mypy): un módulo al que le falte
alguno deja de conformar estructuralmente al Protocol `@runtime_checkable`.
"""

from __future__ import annotations

import inspect

import pytest

from memex.modules import known_modules, resolve
from memex.modules.contract import InterestModule

_SLUGS = known_modules()


def test_registry_not_empty() -> None:
    """Guard: si el registry quedara vacío, los tests parametrizados no probarían nada."""
    assert _SLUGS, "el registry de módulos no debería estar vacío"


@pytest.mark.parametrize("slug", _SLUGS, ids=lambda s: s)
def test_module_satisfies_interest_module(slug: str) -> None:
    """Cada módulo registrado conforma estructuralmente al Protocol (tiene TODOS los miembros)."""
    assert isinstance(resolve(slug)(), InterestModule)


@pytest.mark.parametrize("slug", _SLUGS, ids=lambda s: s)
def test_module_has_uniform_dedup(slug: str) -> None:
    """`dedup` (nombre uniforme) es un miembro obligatorio y es una corrutina."""
    mod = resolve(slug)()
    assert inspect.iscoroutinefunction(mod.dedup), f"{slug}.dedup debe ser async"


@pytest.mark.parametrize("slug", _SLUGS, ids=lambda s: s)
def test_module_has_public_read_door(slug: str) -> None:
    """`read_for_inbox` (la puerta pública del módulo) existe y es invocable."""
    mod = resolve(slug)()
    assert callable(mod.read_for_inbox)


@pytest.mark.parametrize("slug", _SLUGS, ids=lambda s: s)
def test_module_has_forget_door(slug: str) -> None:
    """`forget_inbox` (la puerta de borrado, contraparte del re-extract) existe y es invocable."""
    mod = resolve(slug)()
    assert callable(mod.forget_inbox)
