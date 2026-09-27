"""`clean_sender_display_names`: las fichas de remitente de chat que YA existen con el nombre
visible tal cual («Nombre | Org») pasan al nombre limpio, con alias y pista de organización. Solo
toca fichas de extracción cuyo nombre coincide EXACTAMENTE con el visible de alguno de sus mensajes
(un nombre editado a mano no se pisa). Idempotente y parte de `run_hygiene`."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from memex.modules.identidades.hygiene import clean_sender_display_names, run_hygiene


def _source(conn: Any) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO sources (user_id, name, type, config) "
                "VALUES (1, 'tg-hyg', 'telegram', '{}') RETURNING id"
            )
        ).scalar_one()
    )


def _sender_identity(conn: Any, name: str, tg_id: int, *, source: str = "extraction") -> int:
    iid = int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, source) "
                "VALUES (1, 'persona', :n, :s) RETURNING id"
            ),
            {"n": name, "s": source},
        ).scalar_one()
    )
    conn.execute(
        text(
            "INSERT INTO mod_identidades_identifiers (user_id, identity_id, platform, kind, value, "
            "value_norm) VALUES (1, :i, 'telegram', 'platform_id', :v, :v)"
        ),
        {"i": iid, "v": str(tg_id)},
    )
    return iid


def _inbox(conn: Any, sid: int, tg_id: int, display_name: str, ext: str) -> None:
    payload = {
        "chat_id": 77,
        "message_id": 1,
        "sender": {"user_id": tg_id, "display_name": display_name},
        "text": "pv",
    }
    conn.execute(
        text(
            "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
            "VALUES (1, :s, :e, NOW(), CAST(:p AS JSONB))"
        ),
        {"s": sid, "e": ext, "p": json.dumps(payload)},
    )


def _row(conn: Any, iid: int) -> tuple[str, list[str], dict[str, Any]]:
    r = conn.execute(
        text("SELECT display_name, aliases, metadata FROM mod_identidades WHERE id = :i"),
        {"i": iid},
    ).one()
    return str(r[0]), list(r[1] or []), dict(r[2] or {})


def test_limpia_solo_fichas_con_el_nombre_visible_tal_cual(conn: Any) -> None:
    sid = _source(conn)
    pegado = _sender_identity(conn, "Marta - CoopBlocks", 701)
    _inbox(conn, sid, 701, "Marta - CoopBlocks", "a")
    a_mano = _sender_identity(conn, "Marta Pérez", 702)  # renombrada por el usuario
    _inbox(conn, sid, 702, "Marta - Otra", "b")
    limpio = _sender_identity(conn, "Juan Niebla", 703)
    _inbox(conn, sid, 703, "Juan Niebla", "c")

    renamed = clean_sender_display_names(conn, 1)
    assert [(r.identity_id, r.old_name, r.new_name, r.org_hint) for r in renamed] == [
        (pegado, "Marta - CoopBlocks", "Marta", "CoopBlocks")
    ]
    name, aliases, md = _row(conn, pegado)
    assert name == "Marta" and aliases == ["Marta - CoopBlocks"]
    assert md["org_hint"] == "CoopBlocks" and md["display_name_raw"] == "Marta - CoopBlocks"
    assert _row(conn, a_mano)[0] == "Marta Pérez"
    assert _row(conn, limpio)[0] == "Juan Niebla"
    # idempotente: la segunda pasada no toca nada
    assert clean_sender_display_names(conn, 1) == []


def test_run_hygiene_incluye_a_los_remitentes(conn: Any) -> None:
    sid = _source(conn)
    iid = _sender_identity(conn, "Lucía Reyes 🐲", 801)
    _inbox(conn, sid, 801, "Lucía Reyes 🐲", "z")
    stats = run_hygiene(conn, 1)
    assert [r.identity_id for r in stats.senders] == [iid]
    assert _row(conn, iid)[0] == "Lucía Reyes"


def _plain_identity(conn: Any, name: str, *, source: str, interest: bool = False) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, source, interest) "
                "VALUES (1, 'persona', :n, :s, :i) RETURNING id"
            ),
            {"n": name, "s": source, "i": interest},
        ).scalar_one()
    )


def _mention(conn: Any, iid: int, *, link: str | None = None) -> None:
    meta = json.dumps({"link": link}) if link is not None else "{}"
    conn.execute(
        text(
            "INSERT INTO mod_identidades_mentions (user_id, source_inbox_ids, evidence, "
            "mentioned_name, mentioned_kind, confidence, resolved_kind, resolved_identity_id, "
            "resolution_method, metadata) VALUES (1, '{}', 'e', 'x', 'persona', 0.7, 'persona', "
            ":i, 'exact_name', CAST(:m AS JSONB))"
        ),
        {"i": iid, "m": meta},
    )


def _exists(conn: Any, iid: int) -> bool:
    return (
        conn.execute(text("SELECT 1 FROM mod_identidades WHERE id = :i"), {"i": iid}).scalar()
        is not None
    )


def test_huerfanas_de_extraccion_se_eliminan_y_las_demas_quedan(conn: Any) -> None:
    from memex.modules.identidades.hygiene import drop_orphan_extraction_identities

    huerfana = _plain_identity(conn, "Ponente Citado", source="extraction")
    con_mencion = _plain_identity(conn, "Con Mención", source="extraction")
    _mention(conn, con_mencion)
    manual = _plain_identity(conn, "Puesta A Mano", source="manual")
    de_interes = _plain_identity(conn, "Marcada", source="extraction", interest=True)
    remitente = _sender_identity(conn, "Con Identificador", 901)

    dropped = drop_orphan_extraction_identities(conn, 1)
    assert [(d.identity_id, d.display_name) for d in dropped] == [(huerfana, "Ponente Citado")]
    assert not _exists(conn, huerfana)
    for iid in (con_mencion, manual, de_interes, remitente):
        assert _exists(conn, iid)
    assert drop_orphan_extraction_identities(conn, 1) == []  # idempotente


def _unresolved_mentions(conn: Any) -> int:
    return int(
        conn.execute(
            text(
                "SELECT count(*) FROM mod_identidades_mentions WHERE user_id = 1 "
                "AND mentioned_name = 'x' AND resolution_method = 'unresolved' "
                "AND resolved_identity_id IS NULL"
            )
        ).scalar_one()
    )


def test_fichas_sostenidas_solo_por_menciones_sin_vinculo_se_sueltan(conn: Any) -> None:
    from memex.modules.identidades.hygiene import drop_mention_only_identities

    solo_nombrada = _plain_identity(conn, "Conductor Del Recibo", source="extraction")
    _mention(conn, solo_nombrada, link="mencion")
    _mention(conn, solo_nombrada, link="mencion")
    con_vinculo = _plain_identity(conn, "Asesora Que Firma", source="extraction")
    _mention(conn, con_vinculo, link="firmante")
    _mention(conn, con_vinculo, link="mencion")
    sin_link = _plain_identity(conn, "Mención Vieja", source="extraction")
    _mention(conn, sin_link)  # anterior al registro del vínculo: no se adivina, se sostiene
    manual = _plain_identity(conn, "Manual", source="manual")
    _mention(conn, manual, link="mencion")

    dropped = drop_mention_only_identities(conn, 1)
    assert [(d.identity_id, d.display_name) for d in dropped] == [
        (solo_nombrada, "Conductor Del Recibo")
    ]
    assert not _exists(conn, solo_nombrada)
    assert _unresolved_mentions(conn) == 2  # sus menciones siguen (evidencia), sin identidad
    for iid in (con_vinculo, sin_link, manual):
        assert _exists(conn, iid)
    assert drop_mention_only_identities(conn, 1) == []
    stats = run_hygiene(conn, 1)
    assert stats.mention_only == []
