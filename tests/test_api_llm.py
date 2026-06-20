"""API /llm/consumers — lista de claves/proveedores + upsert por consumidor."""

from __future__ import annotations

from typing import Any


def test_get_lists_keys_and_providers(client: Any) -> None:
    r = client.get("/llm/consumers")
    assert r.status_code == 200
    body = r.json()
    assert "summarizer" in body["consumers"] and "default" in body["consumers"]
    assert set(body["providers"]) == {"deepseek", "anthropic", "codex", "openai"}
    assert body["configured"] == []  # sin filas configuradas todavía


def test_patch_then_get_roundtrip(client: Any) -> None:
    r = client.patch(
        "/llm/consumers/summarizer",
        json={"provider": "codex", "codex_model": "gpt-5.1", "fallback": ["deepseek"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "codex" and body["codex_model"] == "gpt-5.1"
    assert body["fallback"] == ["deepseek"]

    configured = client.get("/llm/consumers").json()["configured"]
    summ = [c for c in configured if c["consumer"] == "summarizer"]
    assert summ and summ[0]["provider"] == "codex"


def test_patch_is_partial(client: Any) -> None:
    client.patch(
        "/llm/consumers/orchestrator",
        json={"provider": "anthropic", "model": "claude-opus-4-8"},
    )
    r = client.patch("/llm/consumers/orchestrator", json={"model": ""})  # limpia el modelo
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "anthropic" and body["model"] is None


def test_patch_invalid_consumer_is_422(client: Any) -> None:
    r = client.patch("/llm/consumers/no-existe", json={"provider": "deepseek"})
    assert r.status_code == 422


def test_patch_invalid_provider_is_422(client: Any) -> None:
    r = client.patch("/llm/consumers/summarizer", json={"provider": "mistral"})
    assert r.status_code == 422


def test_relations_confirm_is_configurable(client: Any) -> None:
    """Regresión: el consumer del job `graph_confirm` debe ser configurable.

    El rename per-message (965e485) movió el call site a `relations_confirm` pero dejó el viejo
    `relations_resolve` (muerto) en `LLM_CONSUMERS`, así que el PATCH del consumer vivo devolvía
    422 y graph-confirm no se podía configurar (caía siempre a `default`/DeepSeek)."""
    consumers = client.get("/llm/consumers").json()["consumers"]
    assert "relations_confirm" in consumers
    assert "relations_resolve" not in consumers
    r = client.patch("/llm/consumers/relations_confirm", json={"provider": "anthropic"})
    assert r.status_code == 200
    assert r.json()["provider"] == "anthropic"
