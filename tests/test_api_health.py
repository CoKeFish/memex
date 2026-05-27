from __future__ import annotations

from typing import Any


def test_healthz(client: Any) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_db_ok(client: Any) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"db": "ok"}
