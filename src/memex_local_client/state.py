"""SQLite local del cliente — estado operativo del daemon.

Dos tablas:

- `plugins`  — qué plugins están instalados/habilitados, con su schedule y
              su última corrida.
- `runs`     — historial de ciclos de ingestión (uno por intento por plugin),
              con stats y error si lo hubo.

Ante un fallo de entrega al gateway NO se bufferea local: la corrida queda en
`error` y la próxima re-fetchea desde la fuente; el dedup server-side
(UNIQUE(source_id, external_id)) absorbe lo repetido.

Los checkpoints (cursores IMAP, etc.) **no viven acá** — viven en memex vía
`/sources/{id}/checkpoint`, igual que para los ingestors del VPS. Decisión
arquitectónica para que un reinstall del PC no rompa el cursor.

Migrations son inline (CREATE TABLE IF NOT EXISTS). No usamos Alembic — la
DB es local, single-writer (el daemon), y el schema cambia raro.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS plugins (
    name           TEXT PRIMARY KEY,
    enabled        INTEGER NOT NULL DEFAULT 0,
    version        TEXT,
    schedule       TEXT,
    source_id      INTEGER,
    installed_at   TEXT NOT NULL,
    last_seen_at   TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    plugin_name    TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,
    posted         INTEGER NOT NULL DEFAULT 0,
    inserted       INTEGER NOT NULL DEFAULT 0,
    duplicates     INTEGER NOT NULL DEFAULT 0,
    errors         INTEGER NOT NULL DEFAULT 0,
    filtered       INTEGER NOT NULL DEFAULT 0,
    error_msg      TEXT
);
CREATE INDEX IF NOT EXISTS runs_by_plugin ON runs(plugin_name, id DESC);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class PluginRow:
    name: str
    enabled: bool
    version: str | None
    schedule: str | None
    source_id: int | None
    installed_at: str
    last_seen_at: str | None


@dataclass(frozen=True)
class RunRow:
    id: int
    plugin_name: str
    started_at: str
    finished_at: str | None
    status: str
    posted: int
    inserted: int
    duplicates: int
    errors: int
    filtered: int
    error_msg: str | None


class State:
    """Wrapper sobre la SQLite local. Single-writer (el daemon)."""

    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """Migraciones inline aditivas para DBs creadas antes de un cambio de schema.

        `CREATE TABLE IF NOT EXISTS` no altera tablas ya existentes, así que las
        columnas nuevas se agregan acá de forma idempotente (no-op si ya están).
        """
        run_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(runs)")}
        if "filtered" not in run_cols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN filtered INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> State:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ---------- plugins ---------- #

    def upsert_plugin(
        self,
        name: str,
        *,
        version: str | None = None,
        schedule: str | None = None,
        source_id: int | None = None,
    ) -> None:
        """Registra o actualiza un plugin. No cambia `enabled`."""
        self._conn.execute(
            """
            INSERT INTO plugins (name, enabled, version, schedule, source_id, installed_at)
            VALUES (?, 0, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                version = COALESCE(excluded.version, plugins.version),
                schedule = COALESCE(excluded.schedule, plugins.schedule),
                source_id = COALESCE(excluded.source_id, plugins.source_id)
            """,
            (name, version, schedule, source_id, _now()),
        )

    def set_enabled(self, name: str, enabled: bool) -> bool:
        cur = self._conn.execute(
            "UPDATE plugins SET enabled = ? WHERE name = ?",
            (1 if enabled else 0, name),
        )
        return cur.rowcount > 0

    def remove_plugin(self, name: str) -> bool:
        cur = self._conn.execute("DELETE FROM plugins WHERE name = ?", (name,))
        return cur.rowcount > 0

    def get_plugin(self, name: str) -> PluginRow | None:
        row = self._conn.execute("SELECT * FROM plugins WHERE name = ?", (name,)).fetchone()
        return _row_to_plugin(row) if row else None

    def list_plugins(self) -> list[PluginRow]:
        rows = self._conn.execute("SELECT * FROM plugins ORDER BY name").fetchall()
        return [_row_to_plugin(r) for r in rows]

    def list_enabled(self) -> list[PluginRow]:
        rows = self._conn.execute(
            "SELECT * FROM plugins WHERE enabled = 1 ORDER BY name"
        ).fetchall()
        return [_row_to_plugin(r) for r in rows]

    def mark_seen(self, name: str) -> None:
        self._conn.execute(
            "UPDATE plugins SET last_seen_at = ? WHERE name = ?",
            (_now(), name),
        )

    # ---------- runs ---------- #

    @contextmanager
    def start_run(self, plugin_name: str) -> Iterator[int]:
        """Crea una fila de run en estado 'running' y la cierra al salir.

        Si el bloque termina sin excepción y `finalize_run` no fue llamado,
        marca como 'unknown' (regression check). El uso normal es:

            with state.start_run("p") as run_id:
                ...trabajo...
                state.finalize_run(run_id, stats=..., status="ok")
        """
        cur = self._conn.execute(
            "INSERT INTO runs (plugin_name, started_at, status) VALUES (?, ?, 'running')",
            (plugin_name, _now()),
        )
        run_id = int(cur.lastrowid or 0)
        try:
            yield run_id
        except Exception as e:
            self.finalize_run(run_id, status="error", error_msg=f"{type(e).__name__}: {e}")
            raise
        else:
            still_running = self._conn.execute(
                "SELECT status FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if still_running and still_running["status"] == "running":
                self.finalize_run(run_id, status="unknown")

    def finalize_run(
        self,
        run_id: int,
        *,
        status: str = "ok",
        posted: int = 0,
        inserted: int = 0,
        duplicates: int = 0,
        errors: int = 0,
        filtered: int = 0,
        error_msg: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, posted = ?, inserted = ?,
                duplicates = ?, errors = ?, filtered = ?, error_msg = ?
            WHERE id = ?
            """,
            (
                _now(),
                status,
                posted,
                inserted,
                duplicates,
                errors,
                filtered,
                error_msg,
                run_id,
            ),
        )

    def recent_runs(self, plugin_name: str | None = None, limit: int = 20) -> list[RunRow]:
        if plugin_name:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE plugin_name = ? ORDER BY id DESC LIMIT ?",
                (plugin_name, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_run(r) for r in rows]


def _row_to_plugin(r: sqlite3.Row) -> PluginRow:
    return PluginRow(
        name=r["name"],
        enabled=bool(r["enabled"]),
        version=r["version"],
        schedule=r["schedule"],
        source_id=r["source_id"],
        installed_at=r["installed_at"],
        last_seen_at=r["last_seen_at"],
    )


def _row_to_run(r: sqlite3.Row) -> RunRow:
    return RunRow(
        id=r["id"],
        plugin_name=r["plugin_name"],
        started_at=r["started_at"],
        finished_at=r["finished_at"],
        status=r["status"],
        posted=r["posted"],
        inserted=r["inserted"],
        duplicates=r["duplicates"],
        errors=r["errors"],
        filtered=r["filtered"],
        error_msg=r["error_msg"],
    )


def open_state(db_path: Path | str | None = None) -> State:
    """Helper para abrir la SQLite con el path por defecto del cliente."""
    if db_path is None:
        from memex_local_client.paths import ensure_layout, state_db_path

        ensure_layout()
        db_path = state_db_path()
    return State(db_path)


__all__ = [
    "PluginRow",
    "RunRow",
    "State",
    "open_state",
]
