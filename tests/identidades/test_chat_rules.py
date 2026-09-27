"""Reglas de chat para el directorio: lo nombrado en un chat no íntimo no gana ficha (determinista,
sin importar el `link` del LLM); en íntimo o fuera de chats sí. Y la re-aplicación sobre lo ya
cargado (`prune_chat_mention_identities`)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import text

from memex.db import connection
from memex.llm import LLMClient
from memex.modules.contract import ModuleContext
from memex.modules.identidades.chat_rules import (
    chat_mode_by_inbox,
    mention_may_create,
    prune_chat_mention_identities,
    strictest_mode,
)
from memex.modules.identidades.module import IdentidadesModule
from memex.modules.identidades.schema import IdentityItem


def _source(stype: str, mode: str | None) -> int:
    cfg: dict[str, Any] = {}
    if mode:
        cfg["allowed_chats"] = [{"chat_id": "g1@g.us", "mode": mode}]
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO sources (user_id, name, type, config) "
                    "VALUES (1, :n, :t, CAST(:cfg AS JSONB)) RETURNING id"
                ),
                {"n": f"{stype}-{uuid4().hex[:6]}", "t": stype, "cfg": json.dumps(cfg)},
            ).scalar_one()
        )


def _inbox(source_id: int, payload: dict[str, Any]) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (1, :s, :e, NOW(), CAST(:p AS JSONB)) RETURNING id"
                ),
                {"s": source_id, "e": uuid4().hex, "p": json.dumps(payload)},
            ).scalar_one()
        )


def _chat_payload(text_: str) -> dict[str, Any]:
    return {
        "chat_id": "g1@g.us",
        "chat_kind": "group",
        "chat_title": "Grupo",
        "message_id": uuid4().hex[:10],
        "sender": {"phone": "57300", "display_name": "Ana", "is_self": False},
        "date": "2026-09-01T12:00:00Z",
        "text": text_,
    }


def _persist(inbox_id: int, items: list[IdentityItem]) -> None:
    async def run() -> None:
        with connection() as conn:
            ctx = ModuleContext(
                user_id=1,
                conn=conn,
                llm=cast(LLMClient, None),
                deps={},
                summary_id=None,
                inbox_ids=(inbox_id,),
            )
            await IdentidadesModule().persist(ctx, items)

    asyncio.run(run())


def _identity_exists(name: str) -> bool:
    with connection() as c:
        return (
            c.execute(
                text("SELECT 1 FROM mod_identidades WHERE user_id = 1 AND display_name = :n"),
                {"n": name},
            ).first()
            is not None
        )


def _mention_method(name: str) -> str:
    with connection() as c:
        return str(
            c.execute(
                text(
                    "SELECT resolution_method FROM mod_identidades_mentions "
                    "WHERE user_id = 1 AND mentioned_name = :n ORDER BY id DESC LIMIT 1"
                ),
                {"n": name},
            ).scalar_one()
        )


def test_pure_helpers() -> None:
    assert strictest_mode(["intimo", "anuncios", None]) == "anuncios"
    assert strictest_mode(["intimo", "programa"]) == "programa"
    assert strictest_mode(["intimo", None]) == "intimo"
    assert strictest_mode([None, None]) is None
    assert mention_may_create(None) and mention_may_create("intimo")
    assert not mention_may_create("anuncios") and not mention_may_create("programa")


def test_chat_mode_by_inbox_reads_allowlist_and_ignores_non_chats() -> None:
    wa = _source("whatsapp", "anuncios")
    mail = _source("imap", None)
    m1 = _inbox(wa, _chat_payload("hola"))
    m2 = _inbox(mail, {"subject": "x", "body_text": "y"})
    with connection() as c:
        modes = chat_mode_by_inbox(c, 1, [m1, m2])
    assert modes == {m1: "anuncios", m2: None}


def test_mention_in_announcements_chat_never_creates_a_card() -> None:
    """Aunque el LLM diga «contraparte», en un chat de anuncios lo nombrado no gana ficha."""
    src = _source("whatsapp", "anuncios")
    mid = _inbox(src, _chat_payload("Charla de Empresa Nueva SAS el jueves"))
    name = f"Empresa Nueva {uuid4().hex[:5]}"
    _persist(
        mid,
        [IdentityItem(link="contraparte", source_inbox_ids=(mid,), name=name, kind="organizacion")],
    )
    assert not _identity_exists(name)
    assert _mention_method(name) == "unresolved"


def test_mention_in_intimate_chat_creates_a_card() -> None:
    src = _source("whatsapp", "intimo")
    mid = _inbox(src, _chat_payload("Robert de Link2Pay nos apoya"))
    name = f"Link2Pay {uuid4().hex[:5]}"
    _persist(
        mid,
        [IdentityItem(link="contraparte", source_inbox_ids=(mid,), name=name, kind="organizacion")],
    )
    assert _identity_exists(name)


def test_prune_reapplies_the_rule_on_existing_cards() -> None:
    """Una ficha que nació de menciones en un chat de anuncios (regla vieja) vuelve a avistamiento;
    una con mención en correo se conserva; en dry-run nada se toca."""
    wa = _source("whatsapp", "anuncios")
    mail = _source("imap", None)
    m_chat = _inbox(wa, _chat_payload("evento de Org Ajena"))
    m_mail = _inbox(mail, {"subject": "x", "body_text": "y"})
    ajena = f"Org Ajena {uuid4().hex[:5]}"
    propia = f"Org Propia {uuid4().hex[:5]}"
    with connection() as c:
        for name, mid in ((ajena, m_chat), (propia, m_mail)):
            iid = int(
                c.execute(
                    text(
                        "INSERT INTO mod_identidades "
                        "(user_id, kind, display_name, source, interest) "
                        "VALUES (1, 'organizacion', :n, 'extraction', FALSE) RETURNING id"
                    ),
                    {"n": name},
                ).scalar_one()
            )
            c.execute(
                text(
                    "INSERT INTO mod_identidades_mentions (user_id, source_inbox_ids, evidence, "
                    "mentioned_name, mentioned_kind, confidence, resolved_kind, "
                    "resolved_identity_id, resolution_method) "
                    "VALUES (1, ARRAY[:m]::bigint[], 'ev', "
                    ":n, 'organizacion', 0.9, 'organizacion', :i, 'created')"
                ),
                {"m": mid, "n": name, "i": iid},
            )
        dry = prune_chat_mention_identities(c, 1, dry_run=True)
        assert ajena in dry.dropped_names and propia not in dry.dropped_names
    assert _identity_exists(ajena)  # dry-run no toca
    with connection() as c:
        st = prune_chat_mention_identities(c, 1)
    assert ajena in st.dropped_names and propia not in st.dropped_names
    assert not _identity_exists(ajena) and _identity_exists(propia)
    assert _mention_method(ajena) == "unresolved"
