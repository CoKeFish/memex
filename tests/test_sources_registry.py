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
