"""Pytest fixtures.

Strategy:
- One session-scoped fixture creates this worker's test database and applies
  migrations once. Under pytest-xdist each worker gets its OWN database
  (`memex_test_gw0`, …) so its per-test TRUNCATE never clobbers another worker.
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
# Base del servidor Postgres de TEST, configurable. El default apunta a la instancia dedicada en
# RAM (servicio `postgres-test` del compose, puerto 5455): los datos de test son efímeros, así que
# correrlos en tmpfs evita el I/O del volumen Docker sobre WSL2 — que en Windows intermitentemente
# se cuelga ~3 min en un commit/CREATE DATABASE y domina el tiempo de cada test. Override con
# MEMEX_TEST_PG (p.ej. en CI, donde el Postgres de servicio ya es efímero) si se usa otra instancia.
_PG_BASE = os.environ.get("MEMEX_TEST_PG", "postgresql+psycopg://memex:memex@localhost:5455")
ADMIN_DB_URL = f"{_PG_BASE}/postgres"
# Bajo pytest-xdist cada worker corre en su propio proceso y recibe PYTEST_XDIST_WORKER
# (gw0, gw1, …) en el entorno ANTES de importar este conftest. Derivamos una DB por worker
# (memex_test_gw0, …) para que el TRUNCATE+reseed por test de un worker no pise a otro. Sin
# xdist la variable no existe → memex_test (corrida secuencial, retro-compatible).
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
_DB_NAME = f"memex_test_{_WORKER}" if _WORKER else "memex_test"
TEST_DB_URL = f"{_PG_BASE}/{_DB_NAME}"
os.environ["MEMEX_DATABASE_URL"] = TEST_DB_URL
os.environ["MEMEX_AUTH_ENFORCED"] = "false"
os.environ["MEMEX_API_TOKEN"] = ""
# El log sink queda INERTE en tests: con `log_persist=True` su escritor por lotes corre en un daemon
# thread e inserta en `log_events`, lo que deadlockea contra el `TRUNCATE ... users ... CASCADE` de
# `_reset_tables` (la FK `log_events.user_id → users` hace que el CASCADE alcance `log_events`).
# Apagarlo no baja cobertura: `test_log_sink` fuerza su estado a mano y `test_api_logs` lo aísla.
os.environ["MEMEX_LOG_PERSIST"] = "false"


@pytest.fixture(scope="session", autouse=True)
def _setup_test_database() -> Iterator[None]:
    """Drop + recreate this worker's DB, then apply migrations once per session."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    admin = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        # `_DB_NAME` es interno/derivado (no input de usuario) → f-string en DDL es seguro,
        # y los identificadores de base de datos no se pueden parametrizar en SQL igualmente.
        # FORCE: si una corrida anterior murió colgada, sus conexiones zombis bloquearían
        # el DROP normal.
        conn.execute(text(f"DROP DATABASE IF EXISTS {_DB_NAME} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {_DB_NAME}"))
        # Cinturón anti-cuelgue: en tests, esperar un lock = bug de aislamiento (p.ej. sembrar
        # vía una transacción abierta y llamar al API, que abre OTRA conexión y se bloquea
        # contra ese lock). Con lock_timeout el statement falla a los 15s con error claro en
        # vez de colgar la suite (y el pre-push) indefinidamente.
        conn.execute(text(f"ALTER DATABASE {_DB_NAME} SET lock_timeout = '15s'"))
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
    import time

    import psycopg.errors
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from memex.db import connection

    # Reintento acotado SOLO ante deadlock detectado: un test previo puede dejar un escritor
    # de fondo (runner/streaming) terminando un INSERT cuya verificación de FK toma RowShare
    # sobre los padres mientras este TRUNCATE pide AccessExclusive → Postgres mata a uno (~1s).
    # Un lock_timeout (cuelgue real) NO se reintenta: debe reventar y señalar el bug.
    for attempt in (1, 2, 3):
        try:
            with connection() as conn:
                conn.execute(
                    text(
                        """
                        TRUNCATE TABLE
                            media_assets, inbox_dedupe_keys, inbox, source_checkpoints,
                            backfill_jobs, filter_rules, geo_place_cache, sources, users
                        RESTART IDENTITY CASCADE
                        """
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO users (id, email, display_name) "
                        "VALUES (1, 'me@local', 'default')"
                    )
                )
                conn.execute(text("SELECT setval(pg_get_serial_sequence('users','id'), 1)"))
            break
        except OperationalError as e:
            if attempt == 3 or not isinstance(e.orig, psycopg.errors.DeadlockDetected):
                raise
            time.sleep(0.2 * attempt)


@pytest.fixture
def conn() -> Iterator[Any]:
    """A managed Connection in a fresh transaction (commits when the test ends).

    OJO: la transacción queda ABIERTA durante todo el test — lo que se escribe por acá es
    INVISIBLE para las conexiones que abre el API (TestClient) y sostiene locks FK sobre las
    filas tocadas. Para sembrar datos que un endpoint debe ver, usar un bloque
    `with connection():` propio (commitea al salir); `conn` sirve para leer de vuelta lo que
    el API ya commiteó.
    """
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
