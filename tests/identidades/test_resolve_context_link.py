"""Principio «propio o para todos» en el resolver contextual: una identidad que en ESTE correo fue
solo nombrada (todas sus menciones con `link = mencion`) no entra al contexto del resolver, así no
se la afilia ni se la funde desde una invitación. Las menciones de remitente y las anteriores al
registro del vínculo (sin `link`) sí cuentan."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.resolve_context import build_email_context
from tests.identidades.test_resolve_context import _identity, _inbox, _source


def _mention(
    inbox_id: int, identity_id: int, *, link: str | None, method: str = "exact_name"
) -> None:
    meta = json.dumps({"link": link}) if link is not None else "{}"
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_identidades_mentions (user_id, source_inbox_ids, mentioned_name, "
                "resolved_identity_id, resolved_kind, resolution_method, metadata) "
                "VALUES (1, ARRAY[:i], 'x', :rid, 'persona', :meth, CAST(:m AS JSONB))"
            ),
            {"i": inbox_id, "rid": identity_id, "meth": method, "m": meta},
        )


def _names(ctx: Any) -> set[str]:
    return {i.display_name for i in ctx.identities}


def test_solo_nombradas_quedan_fuera_del_contexto() -> None:
    src = _source()
    inbox = _inbox(
        src, {"subject": "Invitación al coloquio", "from": {"email": "com@uni.edu", "name": "Com"}}
    )
    ponente = _identity("persona", "Ponente Citado")
    _mention(inbox, ponente, link="mencion")
    asesora = _identity("persona", "Asesora Que Firma")
    _mention(inbox, asesora, link="firmante")
    vieja = _identity("persona", "Mención Vieja")
    _mention(inbox, vieja, link=None)
    remitente = _identity("organizacion", "Com Facultad")
    _mention(inbox, remitente, link=None, method="sender")
    with connection() as c:
        ctx = build_email_context(c, 1, inbox)
    assert ctx is not None
    assert _names(ctx) == {"Asesora Que Firma", "Mención Vieja", "Com Facultad"}


def test_con_un_vinculo_en_otra_mencion_del_mismo_correo_si_entra() -> None:
    src = _source()
    inbox = _inbox(src, {"subject": "Dos menciones", "from": {"email": "a@b.co", "name": "A"}})
    persona = _identity("persona", "Doble")
    _mention(inbox, persona, link="mencion")
    _mention(inbox, persona, link="contraparte")
    with connection() as c:
        ctx = build_email_context(c, 1, inbox)
    assert ctx is not None and "Doble" in _names(ctx)
