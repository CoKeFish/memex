from __future__ import annotations

import pytest

from memex.sources import known_types, resolve


def test_resolve_imap_returns_callable_factory() -> None:
    factory = resolve("imap")
    assert callable(factory), "resolve() must return a SourceFactory (callable)"


def test_resolve_imap_returns_make_source() -> None:
    """The registry binds 'imap' to the make_source function exported by the imap module."""
    from memex.ingestors.imap.source import make_source

    assert resolve("imap") is make_source


def test_resolve_imap_factory_validates_config() -> None:
    """The factory raises SourceConfigError when handed an invalid config."""
    from memex.core.source import SourceConfigError

    factory = resolve("imap")
    with pytest.raises(SourceConfigError):
        factory({})  # missing required keys


def test_resolve_unknown_raises_key_error() -> None:
    with pytest.raises(KeyError, match="no Source implementation registered"):
        resolve("does-not-exist")


def test_known_types_includes_imap() -> None:
    assert "imap" in known_types()


@pytest.mark.parametrize("social_type", ["instagram", "facebook", "x"])
def test_resolve_social_types_return_make_source(social_type: str) -> None:
    """Los tres tipos sociales resuelven a su factory exportada por el módulo social."""
    from memex.ingestors.social.source import (
        make_facebook_source,
        make_instagram_source,
        make_x_source,
    )

    expected = {
        "instagram": make_instagram_source,
        "facebook": make_facebook_source,
        "x": make_x_source,
    }[social_type]
    assert resolve(social_type) is expected


@pytest.mark.parametrize("social_type", ["instagram", "facebook", "x"])
def test_social_factory_validates_config(social_type: str) -> None:
    """factory({}) sin token en env levanta SourceConfigError (config inválida)."""
    from memex.core.source import SourceConfigError

    factory = resolve(social_type)
    with pytest.raises(SourceConfigError):
        factory({"apify_token_env": "DEFINITELY_UNSET_ENV_VAR_XYZ"})


def test_known_types_includes_social() -> None:
    types = known_types()
    assert {"instagram", "facebook", "x"} <= set(types)
