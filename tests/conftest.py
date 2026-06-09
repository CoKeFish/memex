"""Pytest fixtures.

Strategy:
- One session-scoped fixture CLONES this worker's test database from a migrated
  template (`memex_test_template`, built once and rebuilt only when migrations/
  changes). Under pytest-xdist each worker gets its OWN database
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


_TEMPLATE_DB = "memex_test_template"
# Llave arbitraria-pero-fija del advisory lock que serializa la construcción de la plantilla
# entre workers de xdist (procesos independientes): solo el primero construye, el resto espera.
_TEMPLATE_LOCK_KEY = 7453_0001


def _migrations_marker() -> str:
    """Hash del contenido de migrations/ — el sello de vigencia de la plantilla.

    Si CUALQUIER archivo cambia (nueva migración o una editada in-place, ya pasó con la
    0019) el hash cambia y la plantilla se reconstruye. Más estricto que comparar solo el
    head de Alembic.
    """
    import hashlib
    from pathlib import Path

    h = hashlib.sha256()
    for f in sorted(Path("migrations").glob("**/*.py")):
        h.update(f.as_posix().encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:32]


@pytest.fixture(scope="session", autouse=True)
def _setup_test_database() -> Iterator[None]:
    """Clone this worker's DB from a migrated template (built once per migrations change)."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    marker = _migrations_marker()
    admin = create_engine(ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        # Sección crítica cross-worker: el primero construye/reconstruye la plantilla si el
        # marcador no coincide; los demás esperan acá y la encuentran lista. El marcador vive
        # en el COMMENT de la DB (catálogo compartido pg_shdescription) para poder chequearlo
        # desde esta conexión admin SIN conectarse a la plantilla — cualquier backend conectado
        # a ella haría fallar los `CREATE DATABASE ... TEMPLATE` concurrentes de los workers.
        # Se escribe al FINAL de la construcción → una plantilla a medio construir (corrida
        # muerta) nunca pasa por vigente.
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _TEMPLATE_LOCK_KEY})
        try:
            current = conn.execute(
                text(
                    "SELECT shobj_description(oid, 'pg_database') "
                    "FROM pg_database WHERE datname = :n"
                ),
                {"n": _TEMPLATE_DB},
            ).scalar()
            if current != marker:
                # Nombres/marcador internos/derivados (no input de usuario) → f-string en DDL
                # es seguro; identificadores no se pueden parametrizar en SQL igualmente.
                conn.execute(text(f"DROP DATABASE IF EXISTS {_TEMPLATE_DB} WITH (FORCE)"))
                conn.execute(text(f"CREATE DATABASE {_TEMPLATE_DB}"))
                # migrations/env.py lee MEMEX_DATABASE_URL del ENTORNO y pisa `sqlalchemy.url`
                # de la Config → el único canal real es el env var; se apunta temporalmente a
                # la plantilla y se restaura (los tests siguen usando la DB del worker).
                prev_url = os.environ["MEMEX_DATABASE_URL"]
                os.environ["MEMEX_DATABASE_URL"] = f"{_PG_BASE}/{_TEMPLATE_DB}"
                try:
                    command.upgrade(Config("alembic.ini"), "head")
                finally:
                    os.environ["MEMEX_DATABASE_URL"] = prev_url
                # El env.py de alembic puede dejar una conexión pooled viva hacia la plantilla
                # (el engine queda sin dispose); se termina acá o bloquearía los clonados.
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"
                    ),
                    {"n": _TEMPLATE_DB},
                )
                conn.execute(text(f"COMMENT ON DATABASE {_TEMPLATE_DB} IS '{marker}'"))
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _TEMPLATE_LOCK_KEY})

        # Clonado por worker: copia a nivel de archivos (rápida en tmpfs) en vez de correr
        # las ~50 migraciones por worker. Los CREATE concurrentes desde la misma plantilla
        # no se bloquean entre sí (ShareLock es auto-compatible).
        # FORCE: si una corrida anterior murió colgada, sus conexiones zombis bloquearían
        # el DROP normal.
        conn.execute(text(f"DROP DATABASE IF EXISTS {_DB_NAME} WITH (FORCE)"))
        conn.execute(text(f"CREATE DATABASE {_DB_NAME} TEMPLATE {_TEMPLATE_DB}"))
        # Cinturón anti-cuelgue: en tests, esperar un lock = bug de aislamiento (p.ej. sembrar
        # vía una transacción abierta y llamar al API, que abre OTRA conexión y se bloquea
        # contra ese lock). Con lock_timeout el statement falla a los 15s con error claro en
        # vez de colgar la suite (y el pre-push) indefinidamente. Los settings por-database
        # NO se copian con TEMPLATE, así que se setea en cada clon.
        conn.execute(text(f"ALTER DATABASE {_DB_NAME} SET lock_timeout = '15s'"))
    admin.dispose()

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
