from __future__ import annotations

from pathlib import Path
from typing import Any

import respx

from memex.ingestors.runner import RunStats
from memex_local_client.discovery import discover_plugins
from memex_local_client.registry import enable
from memex_local_client.run import execute_plugin, load_plugin_config
from memex_local_client.state import State

_PLUGIN_WITH_TWO_RECORDS = """
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from memex.core.payloads import BasePayload
from memex.core.source import HealthResult, SourceKind, SourceRecord

name = "p2"
version = "0.1.0"
source_type = "fake"
default_schedule = "PT1M"


class _FakeCursor(BaseModel):
    last: str | None = None


class _FakePayload(BasePayload):
    k: int


class _FakeConfig(BaseModel):
    name: str = "p2"


class _S:
    type = "fake"
    kind = SourceKind.EMAIL
    payload_schema = _FakePayload
    config_schema = _FakeConfig
    checkpoint_schema = _FakeCursor

    async def health_check(self) -> HealthResult:
        return HealthResult(status="healthy", detail="ok", checked_at=datetime.now(UTC))

    def fetch(self, checkpoint):
        yield SourceRecord(
            external_id="e1",
            occurred_at=datetime(2026, 5, 27, 10, 0, tzinfo=UTC),
            payload={"k": 1},
            dedupe_keys=[],
        )
        yield SourceRecord(
            external_id="e2",
            occurred_at=datetime(2026, 5, 27, 10, 5, tzinfo=UTC),
            payload={"k": 2},
            dedupe_keys=[],
        )

    def advance_checkpoint(self, checkpoint, last):
        return _FakeCursor(last=last.external_id)


def build_source(local_config: Mapping[str, Any]) -> Any:
    return _S()


def validate_requirements(local_config: Mapping[str, Any]) -> list:
    return []
"""


BASE_URL = "http://localhost:8787"


def test_load_plugin_config_missing_returns_empty(local_home: Path) -> None:
    cfg = load_plugin_config("does-not-exist", local_home / "plugins")
    assert cfg == {}


def test_execute_plugin_calls_state_ingest_and_cursor(plugin_dir_factory: Any) -> None:
    plugin_dir_factory.make("p2", _PLUGIN_WITH_TWO_RECORDS)
    disc = discover_plugins(plugin_dir_factory.root)
    plugin = disc.plugins["p2"]
    state = State(":memory:")
    enable("p2", state, disc.plugins)

    with respx.mock(base_url=BASE_URL) as router:
        router.post("/gateway/plugins/p2/state").respond(
            json={"source_id": 77, "cursor": None, "created": True}
        )
        # 2 records posteados → la API inserta 1 y dropea 1 por filtro.
        ingest_route = router.post("/gateway/plugins/p2/ingest").respond(
            json={"source_id": 77, "inserted": 1, "duplicates": 0, "errors": 0, "filtered": 1}
        )
        router.put("/gateway/plugins/p2/cursor").respond(
            json={"source_id": 77, "cursor": {"last": "e2"}, "created": False}
        )

        stats: RunStats = execute_plugin(
            plugin,
            state=state,
            gateway_url=BASE_URL,
            api_token=None,
            plugins_root=plugin_dir_factory.root,
            chunk_size=10,
            chunk_sleep_ms=0,
        )
        assert stats.posted == 2
        assert stats.inserted == 1
        assert stats.filtered == 1
        assert ingest_route.called

    # El source_id resuelto se cachea en el registry local.
    row = state.get_plugin("p2")
    assert row is not None
    assert row.source_id == 77

    # El run quedó marcado como ok.
    runs = state.recent_runs("p2")
    assert len(runs) == 1
    assert runs[0].status == "ok"
    assert runs[0].posted == 2
    assert runs[0].filtered == 1


def test_execute_plugin_marks_error_on_build_source_failure(plugin_dir_factory: Any) -> None:
    bad_plugin_body = """
from collections.abc import Mapping
from typing import Any

name = "pbad"
version = "0.1.0"
source_type = "fake"
default_schedule = "PT1M"

def build_source(local_config: Mapping[str, Any]) -> Any:
    raise RuntimeError("source construction exploded")

def validate_requirements(local_config: Mapping[str, Any]) -> list:
    return []
"""
    plugin_dir_factory.make("pbad", bad_plugin_body)
    disc = discover_plugins(plugin_dir_factory.root)
    plugin = disc.plugins["pbad"]
    state = State(":memory:")
    enable("pbad", state, disc.plugins)

    try:
        execute_plugin(
            plugin,
            state=state,
            gateway_url=BASE_URL,
            api_token=None,
            plugins_root=plugin_dir_factory.root,
        )
        raise AssertionError("execute_plugin debió relanzar")
    except RuntimeError:
        pass

    runs = state.recent_runs("pbad")
    assert runs[0].status == "error"
    assert "source construction exploded" in (runs[0].error_msg or "")


def test_execute_plugin_marks_error_on_runner_failure(plugin_dir_factory: Any) -> None:
    plugin_dir_factory.make("p2", _PLUGIN_WITH_TWO_RECORDS)
    disc = discover_plugins(plugin_dir_factory.root)
    plugin = disc.plugins["p2"]
    state = State(":memory:")
    enable("p2", state, disc.plugins)

    with respx.mock(base_url=BASE_URL) as router:
        # /state OK, /ingest 500 — el runner re-lanza tras agotar retries
        router.post("/gateway/plugins/p2/state").respond(
            json={"source_id": 77, "cursor": None, "created": True}
        )
        router.post("/gateway/plugins/p2/ingest").respond(500)

        import pytest

        from memex.ingestors.memex_server_client import MemexAPIError

        with pytest.raises(MemexAPIError):
            execute_plugin(
                plugin,
                state=state,
                gateway_url=BASE_URL,
                api_token=None,
                plugins_root=plugin_dir_factory.root,
                chunk_sleep_ms=0,
            )

    runs = state.recent_runs("p2")
    assert runs[0].status == "error"
