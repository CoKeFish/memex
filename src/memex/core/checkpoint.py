import json
from typing import Any

from sqlalchemy import Connection, text


def get_cursor(conn: Connection, source_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT cursor FROM source_checkpoints WHERE source_id = :sid"),
        {"sid": source_id},
    ).first()
    return dict(row[0]) if row else None


def save_cursor(conn: Connection, source_id: int, cursor: dict[str, Any]) -> None:
    """Upsert the checkpoint cursor for a source."""
    conn.execute(
        text(
            """
            INSERT INTO source_checkpoints (source_id, cursor, updated_at)
            VALUES (:sid, CAST(:cursor AS JSONB), NOW())
            ON CONFLICT (source_id) DO UPDATE
              SET cursor = EXCLUDED.cursor,
                  updated_at = NOW()
            """
        ),
        {"sid": source_id, "cursor": json.dumps(cursor)},
    )
