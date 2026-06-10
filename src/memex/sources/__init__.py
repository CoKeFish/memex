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

from memex.core.source import SourceFactory, SourceKind


def _imap_loader() -> SourceFactory:
    from memex.ingestors.imap.source import make_source

    return make_source


def _telegram_loader() -> SourceFactory:
    from memex.ingestors.telegram.source import make_source

    return make_source


def _instagram_loader() -> SourceFactory:
    from memex.ingestors.social.source import make_instagram_source

    return make_instagram_source


def _facebook_loader() -> SourceFactory:
    from memex.ingestors.social.source import make_facebook_source

    return make_facebook_source


def _x_loader() -> SourceFactory:
    from memex.ingestors.social.source import make_x_source

    return make_x_source


_LAZY_FACTORIES: dict[str, Callable[[], SourceFactory]] = {
    "imap": _imap_loader,
    "telegram": _telegram_loader,
    "instagram": _instagram_loader,
    "facebook": _facebook_loader,
    "x": _x_loader,
}

# Categoría conceptual (`SourceKind`) de cada tipo de source. Downstream (módulos de
# extracción) la usa para pre-filtrar por `consumes_kinds` sin tocar el LLM. Para los tipos
# que memex SABE pullear (los de `_LAZY_FACTORIES`) coincide con el ClassVar `kind` de su
# Source —un test de disciplina lo verifica—; pero también incluye tipos que solo se ingieren
# por PUSH (el cliente local), como `outlook`, que no tienen factory de pull pero SÍ categoría:
# un correo es email venga de donde venga.
_KIND_BY_TYPE: dict[str, SourceKind] = {
    "imap": SourceKind.EMAIL,
    "outlook": SourceKind.EMAIL,
    "telegram": SourceKind.CHAT,
    "instagram": SourceKind.SOCIAL,
    "facebook": SourceKind.SOCIAL,
    "x": SourceKind.SOCIAL,
}


def resolve(source_type: str) -> SourceFactory:
    """Return the factory for `source_type`, loading the module lazily.

    Raises `KeyError` if no factory is registered.
    """
    if source_type not in _LAZY_FACTORIES:
        raise KeyError(f"no Source implementation registered for type={source_type!r}")
    return _LAZY_FACTORIES[source_type]()


def kind_for_type(source_type: str) -> SourceKind:
    """Return the conceptual `SourceKind` (email/chat/social) for a source type.

    Mirrors `resolve` but for the category instead of the factory. Raises `KeyError`
    if the type has no registered kind.
    """
    if source_type not in _KIND_BY_TYPE:
        raise KeyError(f"no SourceKind registered for source type={source_type!r}")
    return _KIND_BY_TYPE[source_type]


def known_types() -> list[str]:
    """List source types currently resolvable (pulleables: tienen factory). Útil para
    introspección de la INGESTA. Para enumerar por categoría usar `kind_types()`."""
    return list(_LAZY_FACTORIES.keys())


def kind_types() -> list[str]:
    """Source types con una `SourceKind` registrada — superset de `known_types()`: incluye
    tipos push-only (sin factory de pull) como `outlook`. El work-set de extracción enumera
    por acá, no por `known_types()`, para no saltearse esos mensajes."""
    return list(_KIND_BY_TYPE.keys())


# Tipos cuyo BACKFILL SEGMENTADO (avance por rangos con estado propio) se ofrece. Hoy solo IMAP
# por diseño: las redes ya honran `mode=range` a demanda (ver `_FETCH_MODES_BY_TYPE`), pero el
# backfill segmentado sigue siendo de correo.
_DATE_WINDOW_TYPES: frozenset[str] = frozenset({"imap"})


def supports_date_window(source_type: str) -> bool:
    """True si el tipo es elegible para el backfill segmentado (rangos con estado propio)."""
    return source_type in _DATE_WINDOW_TYPES


#: Modos del fetch a demanda (`POST /sources/{id}/fetch`) que cada tipo honra DE VERDAD. La UI
#: habilita opciones leyendo esto vía `SourceRow.fetch_modes` — nunca hardcodear en el front.
#: telegram solo incremental (su fetch ignora la ventana); push-only (outlook) no se trae.
_FETCH_MODES_BY_TYPE: dict[str, tuple[str, ...]] = {
    "imap": ("incremental", "range", "last"),
    "instagram": ("incremental", "range", "last"),
    "facebook": ("incremental", "range", "last"),
    "x": ("incremental", "range", "last"),
    "telegram": ("incremental",),
}

#: Avisos por modo (server-driven: la UI los muestra tal cual, sin conocer plataformas). Hoy solo
#: el rango de Instagram, que no tiene techo de fecha nativo en el actor.
_FETCH_MODE_CAVEATS: dict[str, dict[str, str]] = {
    "instagram": {
        "range": (
            "Instagram no tiene techo de fecha nativo: el actor escanea desde hoy hacia atrás "
            "hasta «desde» (y se paga ese camino); el «hasta» se filtra en memex. El tope de "
            "cantidad es el freno de costo."
        )
    }
}


def fetch_modes_for_type(source_type: str) -> list[str]:
    """Modos de fetch a demanda que el ingestor honra. [] = no traíble (push-only/desconocido)."""
    return list(_FETCH_MODES_BY_TYPE.get(source_type, ()))


def fetch_mode_caveats_for_type(source_type: str) -> dict[str, str]:
    """Avisos por modo para la UI (p. ej. el costo del rango en Instagram)."""
    return dict(_FETCH_MODE_CAVEATS.get(source_type, {}))
