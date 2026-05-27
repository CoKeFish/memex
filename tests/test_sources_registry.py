from __future__ import annotations

import pytest

from memex.sources import known_types, resolve


def test_resolve_imap_returns_imap_source_class() -> None:
    cls = resolve("imap")
    assert cls.__name__ == "ImapSource"
    assert cls.type == "imap"


def test_resolve_unknown_raises_key_error() -> None:
    with pytest.raises(KeyError, match="no Source implementation registered"):
        resolve("does-not-exist")


def test_known_types_includes_imap() -> None:
    assert "imap" in known_types()
