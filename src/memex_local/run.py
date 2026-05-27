"""Wrapper sobre `memex.ingestors.runner.run_ingestor` con bookkeeping local.

Construye un `BridgeClient` específico para el plugin a ejecutar — ese cliente
es la única superficie HTTP que el cliente local usa contra memex: hace
`POST /bridge/plugins/<name>/state` al arrancar y luego `POST /ingest` +
`PUT /cursor` por chunk. El servidor resuelve `source_id` internamente y
loggea audit events.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from memex.ingestors.bridge_client import BridgeClient
from memex.ingestors.runner import RunStats, run_ingestor
from memex.logging import get_logger
from memex_local.protocol import LocalPlugin
from memex_local.registry import attach_source_id
from memex_local.state import State

_log = get_logger("memex_local.run")


def load_plugin_config(plugin_name: str, plugins_root: Path) -> Mapping[str, Any]:
    """Carga `~/.memex-local/plugins/<name>/config.toml` (vacío si no existe)."""
    cfg_path = plugins_root / plugin_name / "config.toml"
    if not cfg_path.exists():
        return {}
    with cfg_path.open("rb") as f:
        data = tomllib.load(f)
    if not isinstance(data, dict):
        return {}
    return data


def execute_plugin(
    plugin: LocalPlugin,
    *,
    state: State,
    bridge_url: str,
    api_token: str | None,
    plugins_root: Path,
    chunk_size: int = 20,
    chunk_sleep_ms: int = 100,
) -> RunStats:
    """Una corrida de un plugin contra el bridge.

    `bridge_url` y `api_token` se inyectan acá (no un cliente compartido)
    para que cada plugin tenga su propio `BridgeClient` apuntado a su
    namespace `/bridge/plugins/<plugin.name>/`. Cero estado entre plugins.
    """
    log = _log.bind(plugin=plugin.name)
    config = load_plugin_config(plugin.name, plugins_root)

    with state.start_run(plugin.name) as run_id:
        try:
            source = plugin.build_source(config)
        except Exception as e:
            log.exception("memex_local.run.build_source_failed", exc=str(e))
            state.finalize_run(run_id, status="error", error_msg=f"build_source: {e}")
            raise

        client = BridgeClient(
            base_url=bridge_url,
            plugin_name=plugin.name,
            source_type=plugin.source_type,
            api_token=api_token,
        )
        try:
            try:
                stats = run_ingestor(
                    source,
                    source_id=0,  # ignorado por BridgeClient (resuelve via /state)
                    sink=client,
                    chunk_size=chunk_size,
                    chunk_sleep_ms=chunk_sleep_ms,
                )
            except Exception as e:
                log.exception("memex_local.run.runner_failed", exc=str(e))
                state.finalize_run(run_id, status="error", error_msg=f"run_ingestor: {e}")
                raise

            # Después de la primera corrida tenemos el source_id resuelto —
            # lo cacheamos para visibilidad en `memex-local plugin list`.
            if client.resolved_source_id is not None:
                attach_source_id(state, plugin.name, client.resolved_source_id)

            state.finalize_run(
                run_id,
                status="ok",
                posted=stats.posted,
                inserted=stats.inserted,
                duplicates=stats.duplicates,
                errors=stats.errors,
            )
            state.mark_seen(plugin.name)
            log.info(
                "memex_local.run.finished",
                source_id=client.resolved_source_id,
                posted=stats.posted,
                inserted=stats.inserted,
                duplicates=stats.duplicates,
                errors=stats.errors,
                ms_elapsed=stats.ms_elapsed,
            )
            return stats
        finally:
            client.close()
