from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import respx

from memex.ingestors.runner import run_ingestor
from memex_local_client.backfill import (
    BackfillError,
    _CountingSink,
    resolve_window,
    run_backfill,
)
from memex_local_client.connect import bundled_plugins_dir
from memex_local_client.discovery import discover_plugins
from memex_local_client.state import State

BASE = "http://localhost:8787"
NOW = datetime(2026, 6, 15, tzinfo=UTC)


def _plugin(name: str) -> Any:
    root = bundled_plugins_dir()
    assert root is not None
    disc = discover_plugins(root)
    assert name in disc.plugins
    return disc.plugins[name]


# ---- resolve_window ---- #


def test_resolve_window_months_is_calendar() -> None:
    w = resolve_window(months=6, now=NOW)
    assert w.since.date() == date(2025, 12, 15)
    assert w.until.date() == date(2026, 6, 15)


def test_resolve_window_days() -> None:
    w = resolve_window(days=30, now=NOW)
    assert (w.until - w.since).days == 30


def test_resolve_window_since_until_explicit() -> None:
    w = resolve_window(since="2025-01-01", until="2025-03-01")
    assert w.since.date() == date(2025, 1, 1)
    assert w.until.date() == date(2025, 3, 1)


def test_resolve_window_empty_raises() -> None:
    with pytest.raises(BackfillError):
        resolve_window(since="2026-01-01", until="2025-01-01")


def test_resolve_window_needs_a_bound() -> None:
    with pytest.raises(BackfillError):
        resolve_window(now=NOW)


# ---- driver ---- #


def test_backfill_dry_run_counts_without_network(local_home: Path) -> None:
    plugin = _plugin("selftest")
    root = bundled_plugins_dir()
    assert root is not None
    state = State(":memory:")
    # Sin respx: si tocara la red httpx fallaría. El dry-run NO toca red.
    stats = run_backfill(
        plugin,
        gateway_url=BASE,
        api_token=None,
        plugins_root=root,
        window=resolve_window(days=30, now=NOW),
        state=state,
        dry_run=True,
    )
    assert stats.posted == 2
    assert stats.inserted == 0
    assert state.recent_runs("selftest")[0].mode == "backfill"


def test_backfill_real_posts_and_does_not_touch_cursor(local_home: Path) -> None:
    plugin = _plugin("selftest")
    root = bundled_plugins_dir()
    assert root is not None
    state = State(":memory:")
    # assert_all_called=False: registramos la ruta /cursor justamente para PROBAR que NO se llama.
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        # El /state devuelve un cursor existente — el backfill DEBE ignorarlo (no arranca de ahí).
        router.post("/gateway/plugins/selftest/state").respond(
            json={"source_id": 5, "cursor": {"emitted": 99}, "created": False}
        )
        ingest = router.post("/gateway/plugins/selftest/ingest").respond(
            json={"source_id": 5, "inserted": 2, "duplicates": 0, "errors": 0, "filtered": 0}
        )
        cursor = router.put("/gateway/plugins/selftest/cursor").respond(
            json={"source_id": 5, "cursor": {}, "created": False}
        )
        stats = run_backfill(
            plugin,
            gateway_url=BASE,
            api_token=None,
            plugins_root=root,
            window=resolve_window(days=30, now=NOW),
            state=state,
        )
    assert stats.inserted == 2
    assert ingest.called
    assert cursor.called is False  # backfill independiente del cursor incremental


def test_run_ingestor_on_chunk_fires_per_flush() -> None:
    src = _plugin("selftest").build_source({"count": 3})
    seen: list[int] = []
    run_ingestor(
        src,
        source_id=0,
        sink=_CountingSink(),
        chunk_size=2,
        chunk_sleep_ms=0,
        on_chunk=lambda s: seen.append(s.posted),
    )
    assert seen == [2, 3]  # acumulado por chunk flusheado


# ---- ventana en los plugins ---- #


def test_outlook_build_source_honors_backfill_window() -> None:
    src: Any = _plugin("outlook-desktop").build_source(
        {"backfill_since": "2025-12-15", "backfill_until": "2026-06-15"}
    )
    # El piso es la ventana de backfill, ignorando el checkpoint incremental.
    assert src._compute_since(src.checkpoint_schema()) == datetime(2025, 12, 15, tzinfo=UTC)
    assert src.backfill_until == datetime(2026, 6, 15, tzinfo=UTC)


def test_imap_build_source_maps_backfill_to_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNI_IMAP_USER", "alumno@uni.edu")
    monkeypatch.setenv("UNI_IMAP_PASS", "p")
    src: Any = _plugin("imap-university").build_source(
        {
            "server": "imap.test.edu",
            "auth": "basic",
            "username_env": "UNI_IMAP_USER",
            "password_env": "UNI_IMAP_PASS",
            "folders": ["INBOX"],
            "backfill_since": "2025-12-15T00:00:00+00:00",
            "backfill_until": "2026-06-15T00:00:00+00:00",
        }
    )
    assert src.cfg.fetch_mode == "range"
    assert src.cfg.fetch_since == date(2025, 12, 15)
    assert src.cfg.fetch_until == date(2026, 6, 15)
