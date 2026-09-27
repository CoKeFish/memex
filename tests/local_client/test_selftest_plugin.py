from __future__ import annotations

from pathlib import Path
from typing import Any

import respx

from memex.core.payloads import EmailPayload
from memex_local_client.connect import bundled_plugins_dir
from memex_local_client.discovery import discover_plugins
from memex_local_client.run import execute_plugin
from memex_local_client.state import State

BASE = "http://localhost:8787"


def _selftest_plugin() -> Any:
    root = bundled_plugins_dir()
    assert root is not None
    disc = discover_plugins(root)
    assert "selftest" in disc.plugins, (
        f"errors: {[(e.plugin_dir.name, e.reason) for e in disc.errors]}"
    )
    return disc.plugins["selftest"]


def test_selftest_emits_valid_email_payloads() -> None:
    plugin = _selftest_plugin()
    src = plugin.build_source({"count": 2})
    records = list(src.fetch(src.checkpoint_schema()))
    assert len(records) == 2
    for r in records:
        EmailPayload.model_validate(r.payload)  # el payload valida contra el schema email
    assert {r.external_id for r in records} == {"selftest:1", "selftest:2"}


def test_selftest_pushes_through_gateway(local_home: Path) -> None:
    plugin = _selftest_plugin()
    root = bundled_plugins_dir()
    assert root is not None
    state = State(":memory:")
    with respx.mock(base_url=BASE) as router:
        router.post("/gateway/plugins/selftest/state").respond(
            json={"source_id": 9, "cursor": None, "created": True}
        )
        ingest = router.post("/gateway/plugins/selftest/ingest").respond(
            json={"source_id": 9, "inserted": 2, "duplicates": 0, "errors": 0, "filtered": 0}
        )
        router.put("/gateway/plugins/selftest/cursor").respond(
            json={"source_id": 9, "cursor": {"emitted": 2}, "created": False}
        )
        stats = execute_plugin(
            plugin,
            state=state,
            gateway_url=BASE,
            api_token=None,
            plugins_root=root,
            chunk_sleep_ms=0,
        )
    assert stats.posted == 2
    assert stats.inserted == 2
    assert ingest.called
