"""Lookup read-only de resúmenes por inbox (`summaries_for_inboxes`): mapeo en bloque,
lote batch compartido, scope por usuario, mensajes sin resumen ausentes.
"""

from __future__ import annotations

import json

from sqlalchemy import text

from memex.db import connection
from memex.summarizer.lookup import summaries_for_inboxes


def _source(name: str, user_id: int = 1) -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (:u, :n, 'imap') RETURNING id"),
            {"u": user_id, "n": name},
        ).scalar_one()
    return int(sid)


def _inbox(source_id: int, ext: str, user_id: int = 1) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (:u, :sid, :ext, NOW(), CAST(:p AS JSONB)) RETURNING id"
            ),
            {"u": user_id, "sid": source_id, "ext": ext, "p": json.dumps({"body_text": "hola"})},
        ).scalar_one()
    return int(iid)


def _summary(
    inbox_ids: list[int],
    content: str,
    tier: str = "individual",
    user_id: int = 1,
    n: int | None = None,
) -> int:
    metadata = {"n": n} if n is not None else {}
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO summaries (user_id, tier, content, metadata) "
                "VALUES (:u, :t, :c, CAST(:m AS JSONB)) RETURNING id"
            ),
            {"u": user_id, "t": tier, "c": content, "m": json.dumps(metadata)},
        ).scalar_one()
        c.execute(
            text("INSERT INTO summary_inbox_links (summary_id, inbox_id) VALUES (:s, :i)"),
            [{"s": int(sid), "i": i} for i in inbox_ids],
        )
    return int(sid)


def test_mapea_tier_n_y_contenido_en_bloque() -> None:
    src = _source("lk1")
    m1, m2, m3, m4 = (_inbox(src, f"l{i}") for i in range(4))
    sid_lote = _summary([m1, m2, m3], "resumen del lote", tier="batch", n=3)
    sid_ind = _summary([m4], "resumen individual", n=1)
    sin_resumen = _inbox(src, "l9")

    with connection() as c:
        out = summaries_for_inboxes(c, 1, [m1, m2, m4, sin_resumen])
    assert set(out) == {m1, m2, m4}  # m3 no pedido; sin_resumen ausente
    # El resumen batch es LA MISMA fila bajo cada inbox linkeado.
    assert out[m1] == out[m2]
    assert out[m1].summary_id == sid_lote
    assert out[m1].tier == "batch" and out[m1].n == 3
    assert out[m1].content == "resumen del lote"
    assert out[m4].summary_id == sid_ind
    assert out[m4].tier == "individual" and out[m4].n == 1


def test_metadata_sin_n_cae_a_1_e_input_vacio() -> None:
    src = _source("lk2")
    m = _inbox(src, "l10")
    _summary([m], "viejo sin n")  # metadata {} → n=1
    with connection() as c:
        assert summaries_for_inboxes(c, 1, [m])[m].n == 1
        assert summaries_for_inboxes(c, 1, []) == {}


def test_scopea_por_usuario() -> None:
    with connection() as c:
        c.execute(text("INSERT INTO users (id, email, display_name) VALUES (2, 'u2@local', 'u2')"))
    src2 = _source("lk3", user_id=2)
    m2 = _inbox(src2, "l20", user_id=2)
    _summary([m2], "ajeno", user_id=2)
    with connection() as c:
        assert summaries_for_inboxes(c, 1, [m2]) == {}
        assert set(summaries_for_inboxes(c, 2, [m2])) == {m2}
