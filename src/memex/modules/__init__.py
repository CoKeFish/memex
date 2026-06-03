"""Registry de módulos de extracción por intereses (ADR-015).

Mapea `slug` → factory que construye el `InterestModule` concreto. Igual que el registry de
sources (`memex.sources`), los factories se cargan PEREZOSAMENTE: importar el registry no
fuerza a importar las deps de cada módulo (un cliente de MinIO/Drive, un modelo pesado, etc.).

Agregar un módulo nuevo (ADR-015 §8): crear `memex/modules/<slug>/`, su tabla en una migración
del core, y una entrada acá. No se toca el orquestador.

El orquestador SIEMPRE tipa contra `InterestModule` (Protocol), nunca contra la clase concreta
— ver `memex.modules.contract`.
"""

from __future__ import annotations

from collections.abc import Callable

from memex.modules.contract import InterestModule

#: factory de un módulo: construye la instancia concreta (sin args).
ModuleFactory = Callable[[], InterestModule]


def _finance_loader() -> InterestModule:
    from memex.modules.finance.module import FinanceModule

    return FinanceModule()


def _calendar_loader() -> InterestModule:
    from memex.modules.calendar.module import CalendarModule

    return CalendarModule()


def _hackathones_loader() -> InterestModule:
    from memex.modules.hackathones.module import HackathonModule

    return HackathonModule()


_LAZY_FACTORIES: dict[str, Callable[[], ModuleFactory]] = {
    "finance": lambda: _finance_loader,
    "calendar": lambda: _calendar_loader,
    "hackathones": lambda: _hackathones_loader,
}


def resolve(slug: str) -> ModuleFactory:
    """Return the factory for `slug`, loading the module lazily.

    Raises `KeyError` if no module is registered for `slug`.
    """
    if slug not in _LAZY_FACTORIES:
        raise KeyError(f"no InterestModule registered for slug={slug!r}")
    return _LAZY_FACTORIES[slug]()


def known_modules() -> list[str]:
    """List module slugs currently resolvable. Useful for introspection / CLI."""
    return list(_LAZY_FACTORIES.keys())
