"""El remitente de chat nace como UNA persona con el nombre limpio: el visible («Nombre | Org»)
queda como alias y la organización pegada como pista (`metadata.org_hint`). La mención 'sender'
conserva el nombre tal como se vio. Principio «una cosa del mundo, un registro»."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.senders import weave_chat_structure


def _exec(sql: str, **params: Any) -> Any:
    with connection() as c:
        result = c.execute(text(sql), params)
        return result.scalar() if result.returns_rows else None


def _source() -> int:
    return int(
        _exec(
            "INSERT INTO sources (user_id, name, type, config) "
            "VALUES (1, :n, 'telegram', '{\"chat_mode_default\": \"intimo\"}') RETURNING id",
            n="tg-display",
        )
    )


def _inbox(source_id: int, ext: str, tg_id: int, display_name: str) -> int:
    payload = {
        "chat_id": 901,
        "chat_kind": "group",
        "chat_title": "Parche",
        "sender": {"user_id": tg_id, "username": None, "display_name": display_name},
        "message_id": 1,
        "text": "hola",
        "date": "2026-09-01T10:00:00Z",
    }
    return int(
        _exec(
            "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
            "VALUES (1, :sid, :ext, NOW(), CAST(:p AS JSONB)) RETURNING id",
            sid=source_id,
            ext=ext,
            p=json.dumps(payload),
        )
    )


def _identity_row(tg_id: int) -> dict[str, Any]:
    with connection() as c:
        row = (
            c.execute(
                text(
                    """
                SELECT x.display_name, x.aliases, x.metadata
                FROM mod_identidades x JOIN mod_identidades_identifiers f ON f.identity_id = x.id
                WHERE x.user_id = 1 AND f.kind = 'platform_id' AND f.value_norm = :v
                """
                ),
                {"v": str(tg_id)},
            )
            .mappings()
            .one()
        )
    return dict(row)


def test_remitente_con_org_pegada_nace_limpio_con_alias_y_pista() -> None:
    src = _source()
    m1 = _inbox(src, "m1", 4242, "Pablo Vera | GrantBox")
    with connection() as c:
        _, senders, _ = weave_chat_structure(c, 1, [m1])
    assert senders == 1
    row = _identity_row(4242)
    assert row["display_name"] == "Pablo Vera"
    assert row["aliases"] == ["Pablo Vera | GrantBox"]
    assert row["metadata"]["org_hint"] == "GrantBox"
    assert row["metadata"]["display_name_raw"] == "Pablo Vera | GrantBox"
    with connection() as c:
        name = c.execute(
            text(
                "SELECT mentioned_name FROM mod_identidades_mentions "
                "WHERE user_id = 1 AND resolution_method = 'sender' AND :m = ANY(source_inbox_ids)"
            ),
            {"m": m1},
        ).scalar_one()
    assert name == "Pablo Vera | GrantBox"  # la mención conserva lo visto


def test_remitente_con_emoji_nace_sin_emoji_y_sin_metadata_extra() -> None:
    src = _source()
    m1 = _inbox(src, "m1", 4343, "Marty 🤪")
    with connection() as c:
        weave_chat_structure(c, 1, [m1])
    row = _identity_row(4343)
    assert row["display_name"] == "Marty"
    assert row["aliases"] == ["Marty 🤪"]
    assert "org_hint" not in row["metadata"]


def test_nombre_limpio_no_deja_alias_ni_metadata() -> None:
    src = _source()
    m1 = _inbox(src, "m1", 4444, "Juan Niebla")
    with connection() as c:
        weave_chat_structure(c, 1, [m1])
    row = _identity_row(4444)
    assert row["display_name"] == "Juan Niebla"
    assert row["aliases"] == []
    assert row["metadata"] in ({}, None)
