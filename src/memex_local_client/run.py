"""Wrapper sobre `memex.ingestors.runner.run_ingestor` con bookkeeping local.

Construye un `GatewayClient` específico para el plugin a ejecutar — ese cliente
es la única superficie HTTP que el cliente local usa contra memex: hace
`POST /gateway/plugins/<name>/state` al arrancar y luego `POST /ingest` +
`PUT /cursor` por chunk. El servidor resuelve `source_id` internamente y
loggea audit events.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from memex.ingestors.gateway_client import GatewayClient
from memex.ingestors.runner import RunStats, run_ingestor
from memex.logging import get_logger
from memex_local_client.protocol import LocalPlugin, plugin_identity
from memex_local_client.registry import attach_source_id
from memex_local_client.state import State

_log = get_logger("memex_local_client.run")


def load_plugin_config(plugin_name: str, plugins_root: Path) -> Mapping[str, Any]:
    """Carga `~/.memex-local-client/plugins/<name>/config.toml` (vacío si no existe)."""
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
    gateway_url: str,
    api_token: str | None,
    plugins_root: Path,
    chunk_size: int = 20,
    chunk_sleep_ms: int = 100,
) -> RunStats:
    """Una corrida de un plugin contra el gateway.

    `gateway_url` y `api_token` se inyectan acá (no un cliente compartido)
    para que cada plugin tenga su propio `GatewayClient` apuntado a su
    namespace `/gateway/plugins/<plugin.name>/`. Cero estado entre plugins.
    """
    log = _log.bind(plugin=plugin.name)
    config = load_plugin_config(plugin.name, plugins_root)

    # Identidad de la cuenta (p. ej. el email IMAP) que el plugin expone — best-effort: si falla, no
    # se reporta, pero la ingesta sigue. memex la usa para rotular de qué buzón vienen los records.
    try:
        account_email = plugin_identity(plugin, config)
    except Exception as e:
        log.warning("memex_local_client.run.identity_failed", exc=str(e))
        account_email = None

    with state.start_run(plugin.name) as run_id:
        try:
            source = plugin.build_source(config)
        except Exception as e:
            log.exception("memex_local_client.run.build_source_failed", exc=str(e))
            state.finalize_run(run_id, status="error", error_msg=f"build_source: {e}")
            raise

        client = GatewayClient(
            base_url=gateway_url,
            plugin_name=plugin.name,
            source_type=plugin.source_type,
            api_token=api_token,
            account_email=account_email,
        )
        try:
            try:
                stats = run_ingestor(
                    source,
                    source_id=0,  # ignorado por GatewayClient (resuelve via /state)
                    sink=client,
                    chunk_size=chunk_size,
                    chunk_sleep_ms=chunk_sleep_ms,
                )
            except Exception as e:
                log.exception("memex_local_client.run.runner_failed", exc=str(e))
                state.finalize_run(run_id, status="error", error_msg=f"run_ingestor: {e}")
                raise

            # Después de la primera corrida tenemos el source_id resuelto —
            # lo cacheamos para visibilidad en `memex-local-client plugin list`.
            if client.resolved_source_id is not None:
                attach_source_id(state, plugin.name, client.resolved_source_id)

            state.finalize_run(
                run_id,
                status="ok",
                posted=stats.posted,
                inserted=stats.inserted,
                duplicates=stats.duplicates,
                errors=stats.errors,
                filtered=stats.filtered,
            )
            state.mark_seen(plugin.name)
            log.info(
                "memex_local_client.run.finished",
                source_id=client.resolved_source_id,
                posted=stats.posted,
                inserted=stats.inserted,
                duplicates=stats.duplicates,
                errors=stats.errors,
                filtered=stats.filtered,
                ms_elapsed=stats.ms_elapsed,
            )
            return stats
        finally:
            client.close()
