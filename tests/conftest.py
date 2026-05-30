"""Pytest fixtures.

Strategy:
- One session-scoped fixture creates `memex_test` database and applies
  migrations once.
- Per-test fixture truncates all data tables (RESTART IDENTITY) and re-seeds
  user id=1 so each test starts from a known state.
- `MEMEX_DATABASE_URL` is overridden BEFORE memex modules are imported.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

# Pin test DB URL before any memex imports so memex.config picks it up.
ADMIN_DB_URL = "postgresql+psycopg://memex:memex@localhost:5454/postgres"
TEST_DB_URL = "postgresql+psycopg://memex:memex@localhost:5454/memex_test"
os.environ["MEMEX_DATABASE_URL"] = TEST_DB_URL
os.environ["MEMEX_AUTH_ENFORCED"] = "false"
os.environ["MEMEX_API_TOKEN"] = ""


@pytest.fixture(scope="session", autouse=True)
def _setup_test_database() -> Iterator[None]:
    """Drop + recreate memex_test, then apply migrations once per session."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    admin = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS memex_test"))
        conn.execute(text("CREATE DATABASE memex_test"))
    admin.dispose()

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(cfg, "head")

    yield

    from memex.db import get_engine

    get_engine().dispose()


@pytest.fixture(autouse=True)
def _reset_tables() -> None:
    """Per-test cleanup: truncate data tables + re-seed user id=1."""
    from sqlalchemy import text

    from memex.db import connection

    with connection() as conn:
        conn.execute(
            text(
                """
                TRUNCATE TABLE
                    media_assets, inbox_dedupe_keys, inbox, source_checkpoints,
                    filter_rules, sources, users
                RESTART IDENTITY CASCADE
                """
            )
        )
        conn.execute(
            text("INSERT INTO users (id, email, display_name) VALUES (1, 'me@local', 'default')")
        )
        conn.execute(text("SELECT setval(pg_get_serial_sequence('users','id'), 1)"))


@pytest.fixture
def conn() -> Iterator[Any]:
    """A managed Connection in a fresh transaction (autocommits on success)."""
    from memex.db import connection

    with connection() as c:
        yield c


@pytest.fixture
def client() -> Any:
    """FastAPI TestClient with auth NOT enforced.

    NOTE: returned WITHOUT a context manager on purpose. The ASGI lifespan
    (which calls `build_streaming_runner()` + starts the StreamingRunner and
    touches the DB) only runs when the client is used as
    `with TestClient(app) as client:`. Plain `client.get(...)` tests must NOT
    pay that cost or start a Telegram listener — keep this returning the bare
    client. Lifespan startup/shutdown is covered explicitly in
    tests/test_streaming_bootstrap.py.
    """
    from fastapi.testclient import TestClient

    from memex.api.app import app
    from memex.config import settings

    settings.auth_enforced = False
    settings.api_token = ""
    return TestClient(app)


@pytest.fixture
def auth_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """FastAPI TestClient with auth enforced and token='secret-test'."""
    from fastapi.testclient import TestClient

    from memex.api.app import app
    from memex.config import settings

    monkeypatch.setattr(settings, "auth_enforced", True)
    monkeypatch.setattr(settings, "api_token", "secret-test")
    return TestClient(app)


@pytest.fixture
def seed_source() -> dict[str, Any]:
    """Insert a default 'imap' source for user 1, commit, return its row.
    Uses its own connection so TestClient (which opens new connections) sees it.
    """
    from sqlalchemy import text

    from memex.db import connection

    with connection() as c:
        row = (
            c.execute(
                text(
                    "INSERT INTO sources (user_id, name, type) "
                    "VALUES (1, 'imap-test', 'imap') "
                    "RETURNING id, user_id, name, type"
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    return dict(row)


@pytest.fixture
def seed_user2() -> int:
    """Create a second user, commit, return its id."""
    from sqlalchemy import text

    from memex.db import connection

    with connection() as c:
        uid = c.execute(
            text(
                "INSERT INTO users (email, display_name) "
                "VALUES ('other@local', 'other') RETURNING id"
            )
        ).scalar()
    assert uid is not None
    return int(uid)
