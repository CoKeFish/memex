"""CLI social — override de ventana y sink sin checkpoint (range/last no retroceden el cursor)."""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import MagicMock

from memex.ingestors.social.cli import _NoCheckpointSink, _window_override


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {"mode": "incremental", "since": None, "until": None, "limit": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_window_override_incremental_is_empty() -> None:
    assert _window_override(_args()) == {}


def test_window_override_maps_transient_keys() -> None:
    o = _window_override(_args(mode="range", since="2026-01-05", until="2026-02-01", limit=50))
    assert o == {
        "fetch_mode": "range",
        "fetch_since": "2026-01-05",
        "fetch_until": "2026-02-01",
        "fetch_limit": 50,
    }


def test_window_override_last_only_limit() -> None:
    assert _window_override(_args(mode="last", limit=7)) == {"fetch_mode": "last", "fetch_limit": 7}


def test_no_checkpoint_sink_blocks_cursor_persistence() -> None:
    """En range/last el sink NO debe persistir: un backfill de posts viejos retrocedería el
    cursor por-cuenta y el próximo incremental re-pagaría todo desde ahí."""
    inner = MagicMock()
    inner.get_checkpoint.return_value = {"accounts": {}}
    inner.post_ingest_batch.return_value = {
        "inserted": 1,
        "duplicates": 0,
        "errors": 0,
        "filtered": 0,
    }
    sink = _NoCheckpointSink(inner)

    assert sink.get_checkpoint(7) == {"accounts": {}}
    assert sink.post_ingest_batch([{"x": 1}])["inserted"] == 1
    sink.put_checkpoint(7, {"accounts": {"a": {}}})
    inner.put_checkpoint.assert_not_called()
    inner.post_ingest_batch.assert_called_once()
