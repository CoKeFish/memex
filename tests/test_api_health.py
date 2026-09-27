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


def test_version_reports_code_and_schema(client: Any) -> None:
    from memex.version import code_version

    r = client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert body["code_version"] == code_version()
    assert isinstance(body["schema_revision"], str) and body["schema_revision"]
