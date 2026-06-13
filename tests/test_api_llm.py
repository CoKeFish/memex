"""API /llm/consumers — lista de claves/proveedores + upsert por consumidor."""

from __future__ import annotations

from typing import Any


def test_get_lists_keys_and_providers(client: Any) -> None:
    r = client.get("/llm/consumers")
    assert r.status_code == 200
    body = r.json()
    assert "summarizer" in body["consumers"] and "default" in body["consumers"]
    assert set(body["providers"]) == {"deepseek", "anthropic", "codex"}
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
    r = client.patch("/llm/consumers/summarizer", json={"provider": "openai"})
    assert r.status_code == 422
