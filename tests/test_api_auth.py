from __future__ import annotations

from typing import Any


def test_default_client_has_no_auth(client: Any) -> None:
    """Disabled-auth client accepts requests without bearer."""
    assert client.get("/inbox").status_code == 200


def test_enforced_no_bearer_is_401(auth_client: Any) -> None:
    r = auth_client.get("/inbox")
    assert r.status_code == 401
    assert r.json()["detail"] == "missing bearer"


def test_enforced_wrong_bearer_is_403(auth_client: Any) -> None:
    r = auth_client.get("/inbox", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_enforced_correct_bearer_is_200(auth_client: Any) -> None:
    r = auth_client.get("/inbox", headers={"Authorization": "Bearer secret-test"})
    assert r.status_code == 200


def test_health_is_public_under_enforced_auth(auth_client: Any) -> None:
    """Health endpoints don't depend on auth — should still respond."""
    assert auth_client.get("/healthz").status_code == 200
    assert auth_client.get("/readyz").status_code == 200
