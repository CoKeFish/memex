"""Source type registry.

Maps `source.type` strings (as stored in the `sources` table) to a factory
callable that builds a concrete `Source` from a raw config dict.

Factories are loaded lazily — importing the registry does not force the API
process to import ingestor-only dependencies like `imap_tools`. Each factory
loader is just a closure that performs the heavy import on first call.

To add a new source type:

    def _telegram_loader() -> SourceFactory:
        from memex.ingestors.telegram.source import make_source
        return make_source

    _LAZY_FACTORIES["telegram"] = _telegram_loader

See ADR-001 for the isolation rationale.
"""

from __future__ import annotations

from collections.abc import Callable

from memex.core.source import SourceFactory


def _imap_loader() -> SourceFactory:
    from memex.ingestors.imap.source import make_source

    return make_source


_LAZY_FACTORIES: dict[str, Callable[[], SourceFactory]] = {
    "imap": _imap_loader,
}


def resolve(source_type: str) -> SourceFactory:
    """Return the factory for `source_type`, loading the module lazily.

    Raises `KeyError` if no factory is registered.
    """
    if source_type not in _LAZY_FACTORIES:
        raise KeyError(f"no Source implementation registered for type={source_type!r}")
    return _LAZY_FACTORIES[source_type]()


def known_types() -> list[str]:
    """List source types currently resolvable. Useful for introspection."""
    return list(_LAZY_FACTORIES.keys())
