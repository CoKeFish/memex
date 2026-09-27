"""Vectores guardados y vecinos por coseno.

`semantic_vectors`: un vector por (user, slug, ref_id, modelo). `slug` es el módulo dueño del
registro («tareas», «calendar», «identidades»); `ref_id` su id. El texto embebido se guarda con
su hash para no re-embeber lo que no cambió y para auditar qué se comparó.

`neighbors` trae los vectores del ALCANCE (ids que el llamador ya filtró: las tareas vivas de un
asunto, las completadas después de una fecha…) y calcula el coseno en Python. Con decenas o
cientos de filas por alcance es instantáneo; si algún día el alcance son decenas de miles, ahí sí
pgvector. Todo determinista; sin embedder disponible, devuelve vacío y nadie se entera.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.semantic.embedder import Embedder, cosine, get_embedder

_log = get_logger("memex.semantic.store")


def text_hash(s: str) -> str:
    return hashlib.sha256(" ".join(s.split()).lower().encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class Neighbor:
    ref_id: int
    score: float


def upsert_vectors(
    conn: Connection,
    user_id: int,
    slug: str,
    items: Sequence[tuple[int, str]],
    *,
    embedder: Embedder | None = None,
) -> int:
    """Embebe y guarda (ref_id, texto). Salta los que ya tienen vector del mismo texto y modelo.
    Devuelve cuántos calculó. Best-effort: sin embedder no hace nada."""
    emb = embedder or get_embedder()
    if emb is None or not items:
        return 0
    wanted = {int(rid): " ".join(str(t).split()) for rid, t in items if str(t).strip()}
    if not wanted:
        return 0
    existing = {
        int(r[0]): str(r[1])
        for r in conn.execute(
            text(
                "SELECT ref_id, text_hash FROM semantic_vectors "
                "WHERE user_id = :u AND slug = :s AND model = :m AND ref_id = ANY(:ids)"
            ),
            {"u": user_id, "s": slug, "m": emb.model, "ids": list(wanted)},
        ).all()
    }
    todo = [(rid, t) for rid, t in wanted.items() if existing.get(rid) != text_hash(t)]
    if not todo:
        return 0
    vecs = emb.embed([t for _, t in todo])
    conn.execute(
        text(
            """
            INSERT INTO semantic_vectors (user_id, slug, ref_id, model, text_hash, dim, vec)
            VALUES (:u, :s, :r, :m, :h, :d, CAST(:v AS REAL[]))
            ON CONFLICT (user_id, slug, ref_id, model) DO UPDATE
              SET text_hash = EXCLUDED.text_hash, dim = EXCLUDED.dim, vec = EXCLUDED.vec,
                  updated_at = NOW()
            """
        ),
        [
            {
                "u": user_id,
                "s": slug,
                "r": rid,
                "m": emb.model,
                "h": text_hash(t),
                "d": len(v),
                "v": v,
            }
            for (rid, t), v in zip(todo, vecs, strict=True)
        ],
    )
    return len(todo)


def delete_vectors(conn: Connection, user_id: int, slug: str, ref_ids: Sequence[int]) -> int:
    if not ref_ids:
        return 0
    res = conn.execute(
        text(
            "DELETE FROM semantic_vectors WHERE user_id = :u AND slug = :s AND ref_id = ANY(:ids)"
        ),
        {"u": user_id, "s": slug, "ids": [int(i) for i in ref_ids]},
    )
    return int(res.rowcount or 0)


def neighbors(
    conn: Connection,
    user_id: int,
    slug: str,
    query: str,
    *,
    scope_ids: Sequence[int],
    k: int,
    min_score: float,
    embedder: Embedder | None = None,
) -> list[Neighbor]:
    """Los `k` registros del alcance más parecidos a `query` con coseno ≥ `min_score`, de mayor a
    menor. Un id del alcance sin vector guardado (nunca embebido, o de otro modelo) no compite."""
    emb = embedder or get_embedder()
    if emb is None or not scope_ids or not query.strip() or k <= 0:
        return []
    rows = conn.execute(
        text(
            "SELECT ref_id, vec FROM semantic_vectors "
            "WHERE user_id = :u AND slug = :s AND model = :m AND ref_id = ANY(:ids)"
        ),
        {"u": user_id, "s": slug, "m": emb.model, "ids": [int(i) for i in scope_ids]},
    ).all()
    if not rows:
        return []
    q = emb.embed([" ".join(query.split())])[0]
    scored = [
        Neighbor(int(rid), round(cosine(q, [float(x) for x in vec]), 4))
        for rid, vec in rows
        if vec is not None and len(vec) == len(q)
    ]
    scored = [n for n in scored if n.score >= min_score]
    scored.sort(key=lambda n: (-n.score, n.ref_id))
    return scored[:k]


def count_vectors(conn: Connection, user_id: int, slug: str) -> int:
    emb = get_embedder()
    if emb is None:
        return 0
    return int(
        conn.execute(
            text(
                "SELECT count(*) FROM semantic_vectors "
                "WHERE user_id = :u AND slug = :s AND model = :m"
            ),
            {"u": user_id, "s": slug, "m": emb.model},
        ).scalar_one()
    )
