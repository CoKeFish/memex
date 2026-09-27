"""Curador de la jerarquía de identidades: un agente (codex) valida y CONSTRUYE la estructura
definitiva sobre lo que el libro de evidencia dejó en duda.

La parte determinista (`links.py`) acumula reclamos por correo y deriva el padre más sustentado;
lo que NO puede decidir sola queda marcado: padres EN DISPUTA (dos candidatos con soporte),
padres DÉBILES (un solo correo), padres NOMBRADOS sin resolver a una entrada existente, pares de
FUSIÓN en zona gris y RAÍCES sospechosas (creadas por nombre, sin menciones, o basura tipo
«noreply»). El curador toma esos ítems en tandas, arma un expediente por ítem (ficha, dominios,
alias, candidatos con su soporte y los correos que los sustentan) y le pide a codex una decisión
por ítem. Codex puede CONTRASTAR POR INTERNET — SOLO organizaciones y productos; las personas
NUNCA entran al curador (regla del dueño: no perfilar personas en la web) — o inferir mejor a
partir de la evidencia.

Decisiones y cómo se aplican (método `curator`, que PINEA: la derivación automática no las pisa;
el dueño puede re-pinear desde la UI/CLI):
- `keep_parent`   confirma el padre actual (reclamo curator sobre el actual).
- `set_parent`    cuelga del candidato/entrada `parent_id` (anti-ciclo) o de una org nueva por
                  `parent_name` cuando el nombre no existe (única vía que CREA orgs por nombre).
- `no_parent`     la entrada es raíz legítima: se quita el padre y se pinea sin padre.
- `merge`         `absorbed_id` es la misma entidad que `survivor_id` → `merge_identities`
                  (solo org/producto; el libro se re-apunta).
- `delete`        la entrada es basura (un buzón «noreply» como org, un email suelto): se borra
                  si no tiene menciones ni hijos; si los tiene, se deja como raíz pineada.
- `skip`          sin evidencia suficiente ni en la web: no toca nada (queda para el dueño).

Un `codex exec` por tanda (`--output-schema` fuerza el JSON), `web_search_mode=live` opcional.
Costo $0 (suscripción); tokens y latencia se registran en `llm_calls`
(`purpose='identidades_curate'`). `dry_run` arma los expedientes y llama a codex pero NO aplica.
Idempotente: re-correr no re-abre lo pineado (los ítems pineados salen del universo).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import record_llm_call
from memex.db import connection
from memex.logging import get_logger
from memex.modules.identidades.hygiene import run_hygiene
from memex.modules.identidades.links import (
    CLAIM_PERTENECE,
    METHOD_CURATOR,
    LinkNode,
    derive_all,
    hierarchy_nodes,
    list_claims,
    record_claim,
    set_parent,
)
from memex.modules.identidades.merge import merge_identities
from memex.relations.deterministic import weave_pertenencia
from memex.relations.maintenance import reconcile_graph
from memex.webcontext._codex_subprocess import CodexRun, run_codex

_log = get_logger("memex.modules.identidades.curator")

SCOPES = ("disputed", "weak", "unresolved", "candidates", "roots", "all", "tree")
ORG_KINDS = ("organizacion", "producto")
#: Modo árbol: nodos por llamada (el índice completo id→nombre viaja en cada tanda para que las
#: fusiones/padres puedan referenciar cualquier entrada del directorio).
_TREE_BATCH = 120
ACTIONS = ("keep_parent", "set_parent", "no_parent", "merge", "delete", "set_kind", "skip")
KINDS_SETTABLE = ("organizacion", "producto")
_PURPOSE = "identidades_curate"
_DEFAULT_TIMEOUT_S = 600.0  # con web search una tanda de 8 pasaba de 4 min
_DEFAULT_BATCH = 6
_DEFAULT_MIN_CONFIDENCE = 0.75
_EVIDENCE_PER_CANDIDATE = 3
#: Nombres que delatan un buzón/rol tomado por org (señal para el expediente, no una regla dura).
_JUNK_HINTS = ("noreply", "no-reply", "donotreply", "notify", "newsletter", "mailer")


@dataclass(frozen=True)
class CuratorItem:
    """Un ítem del expediente: `key` es el id que el LLM devuelve; `payload` es lo que ve."""

    key: str
    kind: str  # parent | merge | root
    identity_id: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class Decision:
    key: str
    action: str
    parent_id: int | None
    parent_name: str | None
    survivor_id: int | None
    absorbed_id: int | None
    confidence: float
    rationale: str
    kind: str | None = None  # set_kind


@dataclass
class CuratorStats:
    items: int = 0
    batches: int = 0
    decisions: int = 0
    kept: int = 0
    set: int = 0
    created: int = 0
    cleared: int = 0
    merged: int = 0
    deleted: int = 0
    retyped: int = 0
    skipped: int = 0
    dropped: int = 0  # decisiones inválidas o bajo el umbral
    errors: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    rounds: int = 1
    applied: list[dict[str, Any]] = field(default_factory=list)


# --- expediente ---------------------------------------------------------------------------- #


def _identity_card(conn: Connection, user_id: int, identity_id: int) -> dict[str, Any]:
    row = (
        conn.execute(
            text(
                """
                SELECT i.id, i.kind, i.display_name, i.aliases, i.metadata->>'created_by' AS cb,
                       (SELECT count(*) FROM mod_identidades_mentions m
                         WHERE m.resolved_identity_id = i.id) AS mentions,
                       (SELECT count(*) FROM mod_identidades c
                         WHERE c.parent_identity_id = i.id) AS children,
                       (SELECT array_agg(DISTINCT x.kind || ':' || x.value)
                          FROM mod_identidades_identifiers x WHERE x.identity_id = i.id) AS ids,
                       (SELECT array_agg(DISTINCT c.display_name)
                          FROM mod_identidades c WHERE c.parent_identity_id = i.id) AS kids
                FROM mod_identidades i WHERE i.id = :id AND i.user_id = :u
                """
            ),
            {"id": identity_id, "u": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return {"id": identity_id}
    evid = [
        str(r[0])[:160]
        for r in conn.execute(
            text(
                "SELECT evidence FROM mod_identidades_mentions WHERE resolved_identity_id = :id "
                "AND evidence <> '' ORDER BY id DESC LIMIT 2"
            ),
            {"id": identity_id},
        ).all()
    ]
    return {
        "id": int(row["id"]),
        "kind": str(row["kind"]),
        "name": str(row["display_name"]),
        "aliases": list(row["aliases"] or []),
        "identifiers": sorted(str(x) for x in (row["ids"] or [])),
        "mentions": int(row["mentions"] or 0),
        "children_count": int(row["children"] or 0),
        "children": sorted(str(x) for x in (row["kids"] or []))[:12],
        "created_by": row["cb"],
        "evidence": evid,
    }


def _claim_evidence(
    conn: Connection, user_id: int, node_id: int
) -> dict[int | None, list[dict[str, Any]]]:
    """Por destino (id, o None = nombre sin resolver): hasta N correos con asunto/fecha/método."""
    out: dict[int | None, list[dict[str, Any]]] = {}
    for c in list_claims(conn, user_id, source_id=node_id, kind=CLAIM_PERTENECE):
        bucket = out.setdefault(c.target_id, [])
        if len(bucket) >= _EVIDENCE_PER_CANDIDATE:
            continue
        subject = None
        when = None
        if c.inbox_id is not None:
            r = (
                conn.execute(
                    text(
                        "SELECT payload->>'subject' AS s, occurred_at FROM inbox "
                        "WHERE id = :i AND user_id = :u"
                    ),
                    {"i": c.inbox_id, "u": user_id},
                )
                .mappings()
                .first()
            )
            if r is not None:
                subject = (r["s"] or "")[:120]
                when = str(r["occurred_at"])[:10]
        bucket.append(
            {
                "inbox_id": c.inbox_id,
                "subject": subject,
                "date": when,
                "method": c.method,
                "confidence": c.confidence,
                "as_written": c.target_name or None,
                "rationale": c.rationale or None,
            }
        )
    return out


def _parent_item(conn: Connection, user_id: int, node: LinkNode) -> CuratorItem:
    ev = _claim_evidence(conn, user_id, node.id)
    cands = [
        {
            "parent_id": c.target_id,
            "name": c.target_name,
            "messages": c.messages,
            "support": round(c.support, 2),
            "best_confidence": c.best_confidence,
            "methods": list(c.methods),
            "is_current": c.target_id == node.parent_id,
            "evidence": ev.get(c.target_id, []),
        }
        for c in node.candidates
    ]
    if node.parent_id is not None and all(c["parent_id"] != node.parent_id for c in cands):
        cands.insert(
            0,
            {
                "parent_id": node.parent_id,
                "name": node.parent_name,
                "messages": 0,
                "support": 0,
                "best_confidence": 0,
                "methods": [node.parent_source or "?"],
                "is_current": True,
                "evidence": [],
            },
        )
    return CuratorItem(
        key=f"parent:{node.id}",
        kind="parent",
        identity_id=node.id,
        payload={
            "entry": _identity_card(conn, user_id, node.id),
            "current_parent": (
                {"id": node.parent_id, "name": node.parent_name, "source": node.parent_source}
                if node.parent_id is not None
                else None
            ),
            "candidates": cands,
            "unresolved_parent_names": [
                {"name": n, "evidence": ev.get(None, [])} for n in node.unresolved_names
            ],
            "flags": {"disputed": node.disputed, "weak": node.weak},
        },
    )


def touched_since(conn: Connection, user_id: int, since: datetime) -> set[int]:
    """Orgs/productos con novedades desde `since`: creadas o actualizadas, con reclamos nuevos
    (como origen o destino) o con menciones nuevas. Es el delta del modo árbol incremental."""
    rows = conn.execute(
        text(
            """
            SELECT id FROM mod_identidades
             WHERE user_id = :u AND kind IN ('organizacion','producto')
               AND (created_at > :s OR updated_at > :s)
            UNION
            SELECT source_id FROM mod_identidades_link_claims WHERE user_id = :u AND created_at > :s
            UNION
            SELECT target_id FROM mod_identidades_link_claims
             WHERE user_id = :u AND created_at > :s AND target_id IS NOT NULL
            UNION
            SELECT resolved_identity_id FROM mod_identidades_mentions
             WHERE user_id = :u AND created_at > :s AND resolved_identity_id IS NOT NULL
            """
        ),
        {"u": user_id, "s": since},
    ).scalars()
    return {int(x) for x in rows}


def directory_index(conn: Connection, user_id: int) -> list[tuple[int, str]]:
    """`(id, nombre)` de TODAS las orgs/productos: el índice que viaja con cada tanda del modo
    árbol para que las decisiones puedan referenciar cualquier entrada (también en incremental)."""
    return [
        (int(r[0]), str(r[1]))
        for r in conn.execute(
            text(
                "SELECT id, display_name FROM mod_identidades WHERE user_id = :u "
                "AND kind IN ('organizacion','producto') ORDER BY display_name"
            ),
            {"u": user_id},
        ).all()
    ]


def _tree_items(
    conn: Connection, user_id: int, *, since: datetime | None = None
) -> list[CuratorItem]:
    """Modo ÁRBOL (holístico): TODAS las organizaciones/productos, pineadas incluidas, en forma
    compacta (padre actual, alias, dominios, menciones, soporte, pin). El agente ve la estructura
    completa y decide qué cambiaría — duplicados marca/razón social, padres errados, basura, raíces
    que deberían colgar de alguien — en vez de solo juzgar los ítems que un filtro determinista le
    acerca. Es la pasada de «mantener la salud» del árbol; los scopes acotados son incrementales.
    Con `since` es INCREMENTAL: solo las entradas tocadas desde entonces (`touched_since`), con el
    índice completo como contexto."""
    nodes = hierarchy_nodes(conn, user_id)
    if since is not None:
        touched = touched_since(conn, user_id, since)
        nodes = [n for n in nodes if n.id in touched]
    doms: dict[int, list[str]] = {}
    for r in conn.execute(
        text(
            """
            SELECT identity_id, array_agg(DISTINCT d) AS doms FROM (
              SELECT x.identity_id,
                     CASE WHEN x.kind = 'domain' THEN x.value_norm
                          WHEN x.kind = 'email' THEN split_part(x.value_norm, '@', 2)
                     END AS d
              FROM mod_identidades_identifiers x
              WHERE x.user_id = :u AND x.kind IN ('domain', 'email')
            ) t WHERE d IS NOT NULL AND d <> '' GROUP BY identity_id
            """
        ),
        {"u": user_id},
    ).mappings():
        doms[int(r["identity_id"])] = sorted(str(x) for x in (r["doms"] or []))[:4]
    aliases: dict[int, list[str]] = {}
    for r in conn.execute(
        text(
            "SELECT id, aliases FROM mod_identidades WHERE user_id = :u "
            "AND kind IN ('organizacion','producto') AND cardinality(aliases) > 0"
        ),
        {"u": user_id},
    ).mappings():
        aliases[int(r["id"])] = [str(a) for a in (r["aliases"] or [])][:4]
    items: list[CuratorItem] = []
    for n in nodes:
        payload: dict[str, Any] = {
            "id": n.id,
            "kind": n.kind,
            "name": n.display_name,
            "parent_id": n.parent_id,
            "parent_name": n.parent_name,
            "pinned": n.pinned,
            "mentions": n.mention_count,
            "children": n.children_count,
        }
        if n.parent_id is not None:
            payload["parent_support"] = {
                "messages": n.messages,
                "source": n.parent_source,
            }
        if aliases.get(n.id):
            payload["aliases"] = aliases[n.id]
        if doms.get(n.id):
            payload["domains"] = doms[n.id]
        if n.unresolved_names:
            payload["unresolved_parent_names"] = list(n.unresolved_names)
        if n.disputed:
            payload["disputed"] = True
        items.append(
            CuratorItem(key=f"node:{n.id}", kind="node", identity_id=n.id, payload=payload)
        )
    return items


def _merge_items(conn: Connection, user_id: int, limit: int) -> list[CuratorItem]:
    rows = conn.execute(
        text(
            """
            SELECT mc.id, mc.identity_a_id, mc.identity_b_id, mc.reason, mc.score
            FROM mod_identidades_merge_candidates mc
            JOIN mod_identidades a ON a.id = mc.identity_a_id
            JOIN mod_identidades b ON b.id = mc.identity_b_id
            WHERE mc.user_id = :u AND mc.status = 'candidate'
              AND a.kind IN ('organizacion','producto') AND b.kind IN ('organizacion','producto')
            ORDER BY mc.score DESC, mc.id LIMIT :lim
            """
        ),
        {"u": user_id, "lim": limit},
    ).mappings()
    return [
        CuratorItem(
            key=f"merge:{int(r['id'])}",
            kind="merge",
            identity_id=int(r["identity_a_id"]),
            payload={
                "candidate_id": int(r["id"]),
                "a": _identity_card(conn, user_id, int(r["identity_a_id"])),
                "b": _identity_card(conn, user_id, int(r["identity_b_id"])),
                "reason": r["reason"],
                "score": float(r["score"] or 0),
            },
        )
        for r in rows
    ]


#: Menciones mínimas del lado CORTO para proponer un par por contención de nombre (evita ruido de
#: entradas casi vacías; «Salesiana» ⊂ «Pontificia Universidad Salesiana» sí entra).
_CONTAINMENT_MIN_MENTIONS = 3


def _containment_items(conn: Connection, user_id: int, limit: int) -> list[CuratorItem]:
    """Pares org/producto del mismo kind que el difuso pierde y que suelen ser la misma entidad o
    parte↔contenedor, comparados sobre el NÚCLEO del nombre (`org_core`: sin puntuación ni sufijos
    legales, así «Anthropic, PBC» y «Anthropic» sí se ven):
    - `same_core`: núcleo idéntico (razón social vs marca) — se propone siempre, sin mínimo de
      menciones: por definición del directorio deberían ser una sola ficha;
    - `containment`: el núcleo de uno contenido, por tokens, en el del otro (cualquier largo del
      lado corto, a diferencia del barrido de `health` que exige ≥ 2 tokens), con el corto
      suficientemente mencionado.
    Sin decisión previa en la cola. El curador decide merge / set_parent (la larga cuelga de la
    corta) / no; al descartar queda un candidato rechazado para no re-preguntar."""
    rows = conn.execute(
        text(
            """
            WITH pares AS (
              SELECT a.id AS a_id, b.id AS b_id, 'same_core' AS reason
              FROM mod_identidades a
              JOIN mod_identidades b
                ON a.user_id = b.user_id AND a.kind = b.kind AND a.id < b.id
               AND a.org_core = b.org_core
              WHERE a.user_id = :u AND a.kind IN ('organizacion','producto') AND a.org_core <> ''
              UNION ALL
              SELECT a.id, b.id, 'containment'
              FROM mod_identidades a
              JOIN mod_identidades b
                ON a.user_id = b.user_id AND a.kind = b.kind AND a.id <> b.id
              WHERE a.user_id = :u AND a.kind IN ('organizacion','producto')
                AND a.org_core <> '' AND b.org_core <> '' AND a.org_core <> b.org_core
                AND string_to_array(b.org_core, ' ') @> string_to_array(a.org_core, ' ')
                AND cardinality(string_to_array(b.org_core, ' '))
                    > cardinality(string_to_array(a.org_core, ' '))
                AND (SELECT count(*) FROM mod_identidades_mentions m
                      WHERE m.resolved_identity_id = a.id) >= :minm
            )
            SELECT p.a_id, p.b_id, p.reason FROM pares p
            WHERE NOT EXISTS (
              SELECT 1 FROM mod_identidades_merge_candidates mc
              WHERE mc.identity_a_id = LEAST(p.a_id, p.b_id)
                AND mc.identity_b_id = GREATEST(p.a_id, p.b_id)
                AND mc.status <> 'candidate')
            ORDER BY (p.reason = 'same_core') DESC, p.a_id, p.b_id LIMIT :lim
            """
        ),
        {"u": user_id, "minm": _CONTAINMENT_MIN_MENTIONS, "lim": limit},
    ).mappings()
    return [
        CuratorItem(
            key=f"pair:{int(r['a_id'])}:{int(r['b_id'])}",
            kind="merge",
            identity_id=int(r["a_id"]),
            payload={
                "candidate_id": None,
                "a": _identity_card(conn, user_id, int(r["a_id"])),
                "b": _identity_card(conn, user_id, int(r["b_id"])),
                "reason": str(r["reason"]),
                "score": None,
            },
        )
        for r in rows
    ]


def _pair_key(it: CuratorItem) -> tuple[int, int]:
    a, b = int(it.payload["a"]["id"]), int(it.payload["b"]["id"])
    return (min(a, b), max(a, b))


def _is_suspicious_root(node: LinkNode) -> bool:
    if node.parent_id is not None:
        return False
    name = node.display_name.lower()
    if "@" in name or any(h in name for h in _JUNK_HINTS):
        return True
    return node.created_by == "hierarchy_llm" or (
        node.mention_count == 0 and node.children_count > 0
    )


def collect_items(
    conn: Connection,
    user_id: int,
    *,
    scope: str = "all",
    limit: int = 40,
    since: datetime | None = None,
) -> list[CuratorItem]:
    """Los ítems del expediente según `scope`, sin los pineados (ya decididos). Orden: raíces
    sospechosas, nombres sin resolver, disputas, fusiones, débiles — lo más dañino primero."""
    if scope not in SCOPES:
        raise ValueError(f"scope inválido: {scope!r}; válidos: {SCOPES}")
    if scope == "tree":
        # sin `limit`: el árbol se ve entero (o el delta entero) o no sirve
        return _tree_items(conn, user_id, since=since)
    nodes = [n for n in hierarchy_nodes(conn, user_id) if not n.pinned]
    items: list[CuratorItem] = []
    seen: set[int] = set()

    def add_parent(n: LinkNode) -> None:
        if n.id in seen:
            return
        seen.add(n.id)
        items.append(_parent_item(conn, user_id, n))

    if scope in ("roots", "all"):
        for n in nodes:
            if _is_suspicious_root(n):
                add_parent(n)
    if scope in ("unresolved", "all"):
        for n in nodes:
            if n.unresolved_names:
                add_parent(n)
    if scope in ("disputed", "all"):
        for n in nodes:
            if n.disputed:
                add_parent(n)
    if scope in ("candidates", "all"):
        queued = _merge_items(conn, user_id, limit)
        items.extend(queued)
        queued_pairs = {_pair_key(q) for q in queued}
        items.extend(
            it
            for it in _containment_items(conn, user_id, limit)
            if _pair_key(it) not in queued_pairs
        )
    if scope in ("weak", "all"):
        for n in nodes:
            if n.weak:
                add_parent(n)
    return items[:limit]


# --- codex ---------------------------------------------------------------------------------- #

_SYSTEM = """Sos el CURADOR de la jerarquía de organizaciones de un directorio personal.
Recibís ÍTEMS con evidencia extraída de correos del dueño. Para cada ítem devolvés UNA decisión.

Ítems `parent:<id>`: una ORGANIZACIÓN o PRODUCTO (`entry`) y sus padres candidatos («pertenece a»:
área→institución, programa→universidad, producto→empresa, filial→matriz), cada uno con cuántos
correos lo sustentan y esos correos (asunto, fecha, cómo lo escribió el extractor). Puede traer
`unresolved_parent_names` (padres nombrados que no existen en el directorio).
Ítems `merge:<id>` / `pair:<a>:<b>`: dos entradas (`a`, `b`) que PODRÍAN ser la misma
organización/producto.
Ítems `node:<id>` (MODO ÁRBOL): cada organización/producto del directorio, compacta: padre actual
(`parent_id`/`parent_name`, con `parent_support` = correos que lo sustentan y su vía), `aliases`,
`domains`, `mentions`, `children`, `pinned` (padre fijado por el dueño/agente/curador). Ves la
estructura COMPLETA: buscá duplicados (marca vs razón social, variantes del mismo nombre, siglas),
padres incorrectos, entradas que no son organizaciones, raíces que deberían colgar de alguien, y
sub-áreas que deberían colgar de su área intermedia si esta existe. Devolvé decisiones SOLO para lo
que cambiarías (lo que no mencionás se asume correcto); usá keep_parent para FIJAR un padre correcto
que aún no está fijado. Las entradas `pinned` solo se tocan con evidencia clara (explicalo).
En cualquier ítem podés responder `merge` con `survivor_id`/`absorbed_id` de CUALQUIER par de ids
del directorio (uno de los dos debe ser el del ítem).

Acciones (campo `action`):
- keep_parent: el padre actual es correcto.
- set_parent: colgar de `parent_id` (uno de los candidatos u otra entrada citada en el expediente)
  o, SOLO si el contenedor correcto NO existe en el directorio, de `parent_name` (nombre canónico).
- no_parent: es una raíz legítima (una empresa/institución independiente), no cuelga de nada.
- merge: `a` y `b` son la MISMA entidad → `survivor_id` (el nombre canónico) y `absorbed_id`.
  Si NO son la misma pero una PERTENECE a la otra (producto/área vs su empresa/institución),
  respondé set_parent con `parent_id` = el id del contenedor (a o b): la otra quedará colgada.
- delete: la entrada NO es una organización (un buzón «noreply», un email suelto, un asunto).
- set_kind: el tipo está mal: `kind` = "producto" si la entrada es un producto, servicio,
  plataforma o app (Cursor, Loom, YouTube, Battle.net), "organizacion" si es una empresa o
  institución tipada como producto. Que la entrada te COBRE no la vuelve organización: un
  producto puede facturar (Steam, YouTube Premium, Google One). Una EMPRESA adquirida o
  subsidiaria (LinkedIn, GitHub, Blizzard) sigue siendo organización: cuelga de su matriz
  (set_parent), NO se retipa a producto.
- skip: no hay evidencia suficiente para decidir.

Reglas:
- Decidí por la EVIDENCIA y por lo que sabés del mundo; podés buscar en internet para verificar
  a qué institución pertenece un área/programa o si dos nombres son la misma empresa (marca vs
  razón social). NUNCA busques ni infieras nada sobre personas: acá no hay personas.
- «Pertenece a» es CONTENCIÓN institucional, no uso ni proveedor: «Claro» NO pertenece a «Nequi»
  aunque se pague por Nequi; «Telegram» NO pertenece a «Google» aunque se instale de Play.
- merge SOLO cuando las dos entradas nombran el MISMO sujeto: razón social vs nombre comercial de
  la misma organización, traducción, sigla, variante ortográfica o abreviatura («Anthropic» ≡
  «Anthropic, PBC»; «Telegram» ≡ «Telegram FZ-LLC»; «FIB» ≡ «Blockchain Acceleration
  Foundation»; «Salesiana» ≡ «Pontificia Universidad Salesiana»). Al fundir, los nombres de la
  absorbida quedan como alias de la superviviente: no se pierde información.
- Un PRODUCTO, servicio, plataforma o marca comercial de una empresa NO es la empresa: se cuelga
  (set_parent), nunca se funde (Steam → Valve; Claude → Anthropic; Google One → Google; Microsoft
  Forms → Microsoft). Tampoco se funde «porque un nodo tiene más evidencia»: la evidencia decide el
  nombre canónico del superviviente, no si son la misma entidad.
- Un área/carrera/vicerrectoría cuelga de su universidad; una sub-área, de su área si esa área
  existe en el directorio.
- Sé conservador: ante duda real, `skip`. `confidence` 0..1 honesta.
- Respondé SOLO el JSON del esquema; `rationale` en una frase (español)."""


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["decisions"],
        "properties": {
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "item",
                        "action",
                        "parent_id",
                        "parent_name",
                        "survivor_id",
                        "absorbed_id",
                        "kind",
                        "confidence",
                        "rationale",
                    ],
                    "properties": {
                        "item": {"type": "string"},
                        "action": {"type": "string", "enum": list(ACTIONS)},
                        "parent_id": {"type": ["integer", "null"]},
                        "parent_name": {"type": ["string", "null"]},
                        "survivor_id": {"type": ["integer", "null"]},
                        "absorbed_id": {"type": ["integer", "null"]},
                        "kind": {
                            "type": ["string", "null"],
                            "enum": ["organizacion", "producto", None],
                        },
                        "confidence": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                },
            }
        },
    }


def build_prompt(
    items: Sequence[CuratorItem], *, index: Sequence[tuple[int, str]] | None = None
) -> str:
    body = json.dumps(
        [{"item": it.key, "type": it.kind, **it.payload} for it in items],
        ensure_ascii=False,
        indent=1,
        default=str,
    )
    idx = ""
    if index:
        listado = "\n".join(f"{i}: {name}" for i, name in index)
        idx = (
            "\n\nÍNDICE COMPLETO DEL DIRECTORIO (id: nombre) — podés referenciar cualquiera de "
            f"estos ids en parent_id / survivor_id / absorbed_id:\n{listado}\n"
        )
    return f"{_SYSTEM}{idx}\n\nÍTEMS:\n{body}\n"


def parse_decisions(content: str, items: Sequence[CuratorItem]) -> list[Decision]:
    """Parseo ULTRA-DEFENSIVO: solo ítems del lote, acciones del enum, ids enteros; el resto se
    descarta sin tumbar la tanda."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []
    raw = data.get("decisions") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    keys = {it.key for it in items}
    out: list[Decision] = []
    seen: set[str] = set()
    for d in raw:
        if not isinstance(d, dict):
            continue
        key = str(d.get("item", ""))
        action = str(d.get("action", ""))
        if key not in keys or key in seen or action not in ACTIONS:
            continue
        seen.add(key)

        def _int(v: Any) -> int | None:
            return int(v) if isinstance(v, int) and not isinstance(v, bool) else None

        try:
            conf = float(d.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        pname = d.get("parent_name")
        out.append(
            Decision(
                key=key,
                action=action,
                parent_id=_int(d.get("parent_id")),
                parent_name=str(pname).strip()
                if isinstance(pname, str) and pname.strip()
                else None,
                survivor_id=_int(d.get("survivor_id")),
                absorbed_id=_int(d.get("absorbed_id")),
                kind=str(d["kind"]) if d.get("kind") in KINDS_SETTABLE else None,
                confidence=max(0.0, min(1.0, conf)),
                rationale=str(d.get("rationale", ""))[:400],
            )
        )
    return out


async def _ask_codex(
    prompt: str, *, binary: Sequence[str], web_search: bool, timeout_s: float
) -> CodexRun:
    import os

    sandbox = os.environ.get("MEMEX_CODEX_SANDBOX", "read-only")
    with tempfile.TemporaryDirectory(prefix="memex-curator-") as tmp:
        schema_file = Path(tmp) / "schema.json"
        schema_file.write_text(json.dumps(_schema()), encoding="utf-8")
        extra = ["--output-schema", str(schema_file)]
        if web_search:
            extra = ["-c", "web_search_mode=live", *extra]
        return await run_codex(
            binary, prompt, extra_args=extra, sandbox=sandbox, timeout_s=timeout_s
        )


# --- aplicación ----------------------------------------------------------------------------- #


def _kind_of(conn: Connection, user_id: int, ident: int) -> str | None:
    v = conn.execute(
        text("SELECT kind FROM mod_identidades WHERE id = :id AND user_id = :u"),
        {"id": ident, "u": user_id},
    ).scalar()
    return str(v) if v is not None else None


def _would_cycle(conn: Connection, user_id: int, child: int, parent: int) -> bool:
    cur: int | None = parent
    seen: set[int] = set()
    while cur is not None and cur not in seen:
        if cur == child:
            return True
        seen.add(cur)
        cur = conn.execute(
            text("SELECT parent_identity_id FROM mod_identidades WHERE id = :id AND user_id = :u"),
            {"id": cur, "u": user_id},
        ).scalar()
    return False


def _create_org(conn: Connection, user_id: int, name: str) -> int:
    return int(
        conn.execute(
            text(
                """
                INSERT INTO mod_identidades (user_id, kind, display_name, source, metadata)
                VALUES (:u, 'organizacion', :n, 'extraction',
                        jsonb_build_object('created_by', 'curator'))
                RETURNING id
                """
            ),
            {"u": user_id, "n": name},
        ).scalar_one()
    )


def apply_decision(
    conn: Connection, user_id: int, item: CuratorItem, d: Decision, stats: CuratorStats
) -> str:
    """Aplica UNA decisión (misma tx). Devuelve la etiqueta de lo hecho (para el log/stats)."""
    why = f"curador: {d.rationale}" if d.rationale else "curador"
    if item.kind == "merge":
        cand_id = item.payload.get("candidate_id")
        a, b = int(item.payload["a"]["id"]), int(item.payload["b"]["id"])
        if d.action == "set_parent" and d.parent_id in (a, b):
            # «No son la misma entidad: una pertenece a la otra» — la respuesta correcta para
            # «Google» ⊂ «Google Cloud Platform». Se cuelga la otra del padre indicado (reclamo
            # curator, pineado) y el par queda asentado como NO duplicado.
            child = b if d.parent_id == a else a
            if _kind_of(conn, user_id, child) not in ("organizacion", "producto"):
                return "dropped"
            if _would_cycle(conn, user_id, child, d.parent_id):
                return "dropped"
            record_claim(
                conn,
                user_id,
                kind=CLAIM_PERTENECE,
                source_id=child,
                target_id=d.parent_id,
                confidence=d.confidence,
                method=METHOD_CURATOR,
                rationale=why,
            )
            set_parent(conn, user_id, child, d.parent_id, source=METHOD_CURATOR)
            _mark_pair_rejected(
                conn, user_id, a, b, cand_id, d, str(item.payload.get("reason") or "containment")
            )
            stats.set += 1
            return "set"
        if (
            d.action == "merge"
            and d.survivor_id
            and d.absorbed_id
            and d.survivor_id != d.absorbed_id
        ):
            if {d.survivor_id, d.absorbed_id} != {a, b}:
                return "dropped"
            # Se marca ANTES de fundir: la fila del candidato referencia a la absorbida y cae en
            # cascada con su DELETE (queda el rastro en el reclamo/rationale y en llm_calls).
            if cand_id is not None:
                conn.execute(
                    text(
                        "UPDATE mod_identidades_merge_candidates SET status = 'confirmed', "
                        "decided_by = 'curator', confidence = :c, rationale = :r, "
                        "decided_at = NOW() WHERE id = :id"
                    ),
                    {"c": Decimal(str(d.confidence)), "r": d.rationale, "id": cand_id},
                )
            if merge_identities(conn, user_id, d.survivor_id, d.absorbed_id):
                stats.merged += 1
                return "merged"
            return "dropped"
        if d.action in ("skip", "keep_parent", "no_parent"):
            # «no son la misma»: keep/no_parent rechazan el candidato; skip lo deja en cola. Un par
            # por contención (sin fila) se asienta como candidato RECHAZADO para no re-preguntar.
            if cand_id is None or d.action != "skip":
                _mark_pair_rejected(
                    conn,
                    user_id,
                    a,
                    b,
                    cand_id,
                    d,
                    str(item.payload.get("reason") or "containment"),
                )
            stats.skipped += 1
            return "skipped"
        return "dropped"

    child = item.identity_id
    kind = _kind_of(conn, user_id, child)
    if kind not in ORG_KINDS:
        return "dropped:kind"
    if d.action == "skip":
        stats.skipped += 1
        return "skipped"
    if d.action == "merge":
        # «Esta entrada es la MISMA que otra» dicho desde un ítem de padre/nodo. En la 2ª pasada
        # codex lo respondió para «Anthropic» ⊂ «Anthropic, PBC» (0.99) y el aplicador lo
        # descartaba en silencio: la fusión solo se aceptaba en ítems de par.
        surv, absb = d.survivor_id, d.absorbed_id
        if not surv or not absb or surv == absb or child not in (surv, absb):
            return "dropped:merge_ids"
        if (
            _kind_of(conn, user_id, surv) not in ORG_KINDS
            or _kind_of(conn, user_id, absb) not in ORG_KINDS
        ):
            return "dropped:merge_kind"
        if merge_identities(conn, user_id, surv, absb):
            stats.merged += 1
            return "merged"
        return "dropped:merge_failed"
    if d.action == "set_kind":
        if d.kind not in KINDS_SETTABLE:
            return "dropped:kind_value"
        if d.kind == kind:
            return "dropped:kind_same"
        conn.execute(
            text(
                "UPDATE mod_identidades SET kind = :k, notes = CASE WHEN notes = '' THEN :w "
                "ELSE notes END, updated_at = NOW() WHERE id = :id AND user_id = :u"
            ),
            {"k": d.kind, "w": why[:300], "id": child, "u": user_id},
        )
        # El vértice cambia de slug: se re-tejen sus aristas de pertenencia (la suya y las de sus
        # hijos); las del slug viejo las poda `reconcile_graph` al cierre de la tanda.
        weave_pertenencia(conn, user_id, child)
        for cid in conn.execute(
            text("SELECT id FROM mod_identidades WHERE parent_identity_id = :p AND user_id = :u"),
            {"p": child, "u": user_id},
        ).scalars():
            weave_pertenencia(conn, user_id, int(cid))
        stats.retyped += 1
        return "retyped"
    if d.action == "keep_parent":
        cur = conn.execute(
            text("SELECT parent_identity_id FROM mod_identidades WHERE id = :id"), {"id": child}
        ).scalar()
        if cur is None:
            return "dropped"
        record_claim(
            conn,
            user_id,
            kind=CLAIM_PERTENECE,
            source_id=child,
            target_id=int(cur),
            confidence=d.confidence,
            method=METHOD_CURATOR,
            rationale=why,
        )
        set_parent(conn, user_id, child, int(cur), source=METHOD_CURATOR)
        stats.kept += 1
        return "kept"
    if d.action == "no_parent":
        set_parent(conn, user_id, child, None, source=METHOD_CURATOR)
        conn.execute(
            text(
                "UPDATE mod_identidades SET notes = CASE WHEN notes = '' THEN :w "
                "ELSE notes END WHERE id = :id AND user_id = :u"
            ),
            {"w": why[:300], "id": child, "u": user_id},
        )
        stats.cleared += 1
        return "cleared"
    if d.action == "set_parent":
        parent = d.parent_id
        created = False
        if parent is None and d.parent_name:
            from memex.modules.identidades.links import resolve_target_name

            parent = resolve_target_name(conn, user_id, d.parent_name, exclude_id=child)
            if parent is None:
                parent = _create_org(conn, user_id, d.parent_name)
                created = True
        if parent is None or parent == child:
            return "dropped"
        if _kind_of(conn, user_id, parent) not in ("organizacion", "producto"):
            return "dropped"
        if _would_cycle(conn, user_id, child, parent):
            return "dropped"
        record_claim(
            conn,
            user_id,
            kind=CLAIM_PERTENECE,
            source_id=child,
            target_id=parent,
            target_name=d.parent_name or "",
            confidence=d.confidence,
            method=METHOD_CURATOR,
            rationale=why,
        )
        set_parent(conn, user_id, child, parent, source=METHOD_CURATOR)
        stats.set += 1
        if created:
            stats.created += 1
        return "created" if created else "set"
    if d.action == "delete":
        card = item.payload.get("entry", {})
        has_evidence = int(card.get("mentions", 0) or 0) > 0
        has_children = int(card.get("children_count", 0) or 0) > 0
        if has_evidence or has_children:
            # con evidencia o hijos no se borra: queda raíz pineada, el dueño decide.
            set_parent(conn, user_id, child, None, source=METHOD_CURATOR)
            stats.cleared += 1
            return "cleared"
        conn.execute(
            text("DELETE FROM mod_identidades WHERE id = :id AND user_id = :u"),
            {"id": child, "u": user_id},
        )
        stats.deleted += 1
        return "deleted"
    return "dropped"


def _mark_pair_rejected(
    conn: Connection,
    user_id: int,
    a: int,
    b: int,
    cand_id: int | None,
    d: Decision,
    reason: str = "containment",
) -> None:
    """Asienta «no son la misma entidad»: actualiza el candidato de la cola si existe, o inserta uno
    rechazado (`containment`) para que el par no se vuelva a preguntar."""
    if cand_id is not None:
        conn.execute(
            text(
                "UPDATE mod_identidades_merge_candidates SET status = 'rejected', "
                "decided_by = 'curator', confidence = :c, rationale = :r, "
                "decided_at = NOW() WHERE id = :id"
            ),
            {"c": Decimal(str(d.confidence)), "r": d.rationale, "id": cand_id},
        )
        return
    conn.execute(
        text(
            """
            INSERT INTO mod_identidades_merge_candidates
              (user_id, identity_a_id, identity_b_id, reason, score, status,
               decided_by, confidence, rationale, decided_at)
            VALUES (:u, :a, :b, :reason, NULL, 'rejected', 'curator', :c, :r, NOW())
            ON CONFLICT (identity_a_id, identity_b_id) DO NOTHING
            """
        ),
        {
            "u": user_id,
            "a": min(a, b),
            "b": max(a, b),
            "reason": reason,
            "c": Decimal(str(d.confidence)),
            "r": d.rationale,
        },
    )


def apply_decisions(
    conn: Connection,
    user_id: int,
    items: Sequence[CuratorItem],
    decisions: Sequence[Decision],
    stats: CuratorStats,
    *,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
) -> None:
    by_key = {it.key: it for it in items}
    for d in decisions:
        it = by_key.get(d.key)
        if it is None:
            continue
        stats.decisions += 1
        if d.action != "skip" and d.confidence < min_confidence:
            stats.dropped += 1
            stats.applied.append({"item": d.key, "action": d.action, "result": "low_confidence"})
            continue
        # SAVEPOINT por decisión: una que falle (bug, FK, dato raro) se revierte sola y no tumba
        # las demás de la tanda ni deja la tx abortada.
        try:
            with conn.begin_nested():
                result = apply_decision(conn, user_id, it, d, stats)
        except Exception as e:  # best-effort por decisión
            stats.errors += 1
            result = "error"
            _log.warning(
                "identidades.curator.decision_failed",
                item=d.key,
                action=d.action,
                exc_type=type(e).__name__,
                exc_msg=str(e),
            )
        if result.startswith("dropped"):
            stats.dropped += 1
        stats.applied.append(
            {
                "item": d.key,
                "action": d.action,
                "result": result,
                "parent_id": d.parent_id,
                "parent_name": d.parent_name,
                "confidence": d.confidence,
                "rationale": d.rationale,
            }
        )
        _log.info(
            "identidades.curator.decision",
            item=d.key,
            action=d.action,
            result=result,
            confidence=d.confidence,
        )


# --- runner --------------------------------------------------------------------------------- #


def _resolve_binary() -> list[str]:
    resolved = shutil.which("codex")
    if resolved is None:
        raise RuntimeError("binario `codex` no encontrado en PATH (¿npm i -g @openai/codex?)")
    return [resolved]


def _changes(stats: CuratorStats) -> int:
    return (
        stats.kept
        + stats.set
        + stats.created
        + stats.cleared
        + stats.merged
        + stats.deleted
        + stats.retyped
    )


async def run_curator(
    user_id: int,
    *,
    scope: str = "all",
    limit: int = 40,
    batch_size: int = _DEFAULT_BATCH,
    web_search: bool = True,
    dry_run: bool = False,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    rounds: int = 1,
    since: datetime | None = None,
    binary: Sequence[str] | None = None,
    ask: Any | None = None,
) -> CuratorStats:
    """Cura la jerarquía: arma el expediente (`scope`), pregunta a codex por tandas y aplica.
    `rounds` > 1 repite la pasada mientras la anterior haya cambiado algo (codex no es determinista:
    una sola pasada deja huecos; dos o tres convergen). `since` (solo `tree`) = modo incremental:
    solo lo tocado desde esa fecha. Al terminar una pasada árbol NO dry-run se guarda la marca de
    agua (`last_tree_at`; `last_full_tree_at` si fue completa) en `module_settings.config`.
    `ask(prompt) -> CodexRun` inyectable (tests con fake); `dry_run` no aplica ni pinea (y por eso
    corre una sola ronda)."""
    total = CuratorStats()
    for r in range(max(1, rounds)):
        st = await _run_once(
            user_id,
            scope=scope,
            limit=limit,
            batch_size=batch_size,
            web_search=web_search,
            dry_run=dry_run,
            min_confidence=min_confidence,
            timeout_s=timeout_s,
            since=since,
            binary=binary,
            ask=ask,
        )
        total.items += st.items
        total.batches += st.batches
        total.decisions += st.decisions
        total.kept += st.kept
        total.set += st.set
        total.created += st.created
        total.cleared += st.cleared
        total.merged += st.merged
        total.deleted += st.deleted
        total.retyped += st.retyped
        total.skipped += st.skipped
        total.dropped += st.dropped
        total.errors += st.errors
        total.llm_calls += st.llm_calls
        total.prompt_tokens += st.prompt_tokens
        total.completion_tokens += st.completion_tokens
        total.latency_ms += st.latency_ms
        total.applied.extend({**a, "round": r + 1} for a in st.applied)
        total.rounds = r + 1
        if dry_run or _changes(st) == 0 or st.items == 0:
            break
    if scope == "tree" and not dry_run:
        # Marca al TERMINAR: lo que esta pasada cambió no vuelve a aparecer como «tocado» en el
        # próximo incremental; la pasada completa semanal cubre lo que cambiara mientras corría.
        with connection() as conn:
            _set_marks(conn, user_id, datetime.now(UTC), full=since is None)
    return total


async def _run_once(
    user_id: int,
    *,
    scope: str,
    limit: int,
    batch_size: int,
    web_search: bool,
    dry_run: bool,
    min_confidence: float,
    timeout_s: float,
    since: datetime | None,
    binary: Sequence[str] | None,
    ask: Any | None,
) -> CuratorStats:
    stats = CuratorStats()
    with connection() as conn:
        items = collect_items(conn, user_id, scope=scope, limit=limit, since=since)
        index_all = directory_index(conn, user_id) if scope == "tree" else None
    stats.items = len(items)
    if not items:
        _log.info("identidades.curator.empty", user_id=user_id, scope=scope)
        return stats
    bin_ = list(binary) if binary else (None if ask else _resolve_binary())
    index: list[tuple[int, str]] | None = None
    if scope == "tree":
        batch_size = _TREE_BATCH
        index = index_all  # el directorio COMPLETO, también en incremental
    for start in range(0, len(items), max(1, batch_size)):
        batch = items[start : start + batch_size]
        prompt = build_prompt(batch, index=index)
        try:
            if ask is not None:
                run: CodexRun = await ask(prompt)
            else:
                assert bin_ is not None
                run = await _ask_codex(
                    prompt, binary=bin_, web_search=web_search, timeout_s=timeout_s
                )
        except Exception as e:  # best-effort por tanda
            stats.errors += 1
            _log.warning("identidades.curator.batch_failed", error=str(e), items=len(batch))
            continue
        stats.batches += 1
        stats.llm_calls += 1
        stats.prompt_tokens += run.usage.input_tokens
        stats.completion_tokens += run.usage.output_tokens
        stats.latency_ms += run.latency_ms
        decisions = parse_decisions(run.last_message, batch)
        record_llm_call(
            user_id=user_id,
            purpose=_PURPOSE,
            model="codex/default",
            prompt_tokens=run.usage.input_tokens,
            completion_tokens=run.usage.output_tokens,
            cache_hit_tokens=run.usage.cached_input_tokens,
            cost_usd=Decimal("0"),
            latency_ms=run.latency_ms,
            status="ok",
            metadata={
                "scope": scope,
                "mode": "incremental" if since is not None else "full",
                "items": [it.key for it in batch],
                "decisions": len(decisions),
                "dry_run": dry_run,
                "web_search": web_search,
            },
            response_text=run.last_message[:4000],
        )
        if dry_run:
            stats.decisions += len(decisions)
            stats.applied.extend(
                {
                    "item": d.key,
                    "action": d.action,
                    "result": "dry_run",
                    "parent_id": d.parent_id,
                    "parent_name": d.parent_name,
                    "survivor_id": d.survivor_id,
                    "absorbed_id": d.absorbed_id,
                    "confidence": d.confidence,
                    "rationale": d.rationale,
                }
                for d in decisions
            )
            continue
        with connection() as conn:
            before = stats.retyped
            apply_decisions(conn, user_id, batch, decisions, stats, min_confidence=min_confidence)
            if stats.retyped > before:
                reconcile_graph(conn, user_id)
    _log.info(
        "identidades.curator.end",
        user_id=user_id,
        scope=scope,
        items=stats.items,
        batches=stats.batches,
        kept=stats.kept,
        set=stats.set,
        created=stats.created,
        cleared=stats.cleared,
        merged=stats.merged,
        deleted=stats.deleted,
        retyped=stats.retyped,
        skipped=stats.skipped,
        dropped=stats.dropped,
        errors=stats.errors,
    )
    return stats


# --- marcas de agua y mantenimiento automático ------------------------------------------- #

_MARK_LAST_TREE = "last_tree_at"
_MARK_LAST_FULL = "last_full_tree_at"
#: Cada cuántos días la pasada de mantenimiento vuelve a mirar el árbol COMPLETO (no solo el delta).
FULL_TREE_EVERY_DAYS = 7


def get_marks(conn: Connection, user_id: int) -> dict[str, datetime | None]:
    """Marcas de agua del curador (`module_settings.config.curator`): última pasada árbol y última
    pasada árbol COMPLETA. None si nunca corrió."""
    row = conn.execute(
        text(
            "SELECT config->'curator' FROM module_settings "
            "WHERE user_id = :u AND module_slug = 'identidades'"
        ),
        {"u": user_id},
    ).scalar()
    marks = row if isinstance(row, dict) else {}
    out: dict[str, datetime | None] = {}
    for k in (_MARK_LAST_TREE, _MARK_LAST_FULL):
        v = marks.get(k)
        out[k] = datetime.fromisoformat(str(v)) if v else None
    return out


def _set_marks(conn: Connection, user_id: int, when: datetime, *, full: bool) -> None:
    keys = [_MARK_LAST_TREE] + ([_MARK_LAST_FULL] if full else [])
    for k in keys:
        conn.execute(
            text(
                """
                INSERT INTO module_settings (user_id, module_slug, enabled, config)
                VALUES (:u, 'identidades', TRUE,
                        jsonb_build_object('curator',
                                           jsonb_build_object(CAST(:k AS TEXT), CAST(:v AS TEXT))))
                ON CONFLICT (user_id, module_slug) DO UPDATE SET
                  config = jsonb_set(
                    jsonb_set(COALESCE(module_settings.config, '{}'::jsonb), '{curator}',
                              COALESCE(module_settings.config->'curator', '{}'::jsonb)),
                    ARRAY['curator', CAST(:k AS TEXT)], to_jsonb(CAST(:v AS TEXT)))
                """
            ),
            {"u": user_id, "k": k, "v": when.isoformat()},
        )


@dataclass
class MaintenanceStats:
    """Resultado de una pasada de mantenimiento (job `identidades_curate` / CLI `maintain`)."""

    hygiene_aliases: int = 0
    hygiene_domains: int = 0
    derived_changed: int = 0
    targeted: dict[str, Any] = field(default_factory=dict)
    tree_mode: str = "skipped"  # incremental | full | skipped
    tree: dict[str, Any] = field(default_factory=dict)
    errors: int = 0
    steps_failed: list[str] = field(default_factory=list)


def _light(stats: CuratorStats) -> dict[str, Any]:
    d = asdict(stats)
    d["applied"] = len(stats.applied)
    return d


async def run_curator_maintenance(
    user_id: int,
    *,
    full_every_days: int = FULL_TREE_EVERY_DAYS,
    ask: Any | None = None,
) -> MaintenanceStats:
    """La pasada AUTOMÁTICA de salud de la jerarquía, pensada para correr a diario:
    1. higiene (alias en colisión, dominios no atribuibles) + derivación desde el libro (sin LLM);
    2. curador acotado (`all`: disputas, débiles, nombres sin resolver, pares, raíces) — incremental
       por construcción: lo pineado no vuelve a entrar;
    3. árbol INCREMENTAL (solo lo tocado desde la última pasada árbol), o árbol COMPLETO con 2
       rondas si nunca corrió o la última completa tiene más de `full_every_days` días.
    Best-effort por paso. Costo $0 (codex); duración: segundos el 1-2, minutos el 3."""
    ms = MaintenanceStats()
    try:
        with connection() as conn:
            hs = run_hygiene(conn, user_id)
            ms.hygiene_aliases = len(hs.aliases)
            ms.hygiene_domains = len(hs.domains)
            ms.derived_changed = len(derive_all(conn, user_id))
    except Exception as e:  # best-effort
        ms.errors += 1
        ms.steps_failed.append("hygiene")
        _log.warning("identidades.maintenance.step_failed", step="hygiene", error=str(e))
    try:
        ms.targeted = _light(await run_curator(user_id, scope="all", limit=40, ask=ask))
    except Exception as e:
        ms.errors += 1
        ms.steps_failed.append("targeted")
        _log.warning("identidades.maintenance.step_failed", step="targeted", error=str(e))
    try:
        with connection() as conn:
            marks = get_marks(conn, user_id)
        last_full = marks[_MARK_LAST_FULL]
        last_tree = marks[_MARK_LAST_TREE]
        due_full = last_full is None or (
            datetime.now(UTC) - last_full > timedelta(days=full_every_days)
        )
        if due_full:
            ms.tree_mode = "full"
            ms.tree = _light(await run_curator(user_id, scope="tree", rounds=2, ask=ask))
        else:
            ms.tree_mode = "incremental"
            ms.tree = _light(await run_curator(user_id, scope="tree", since=last_tree, ask=ask))
    except Exception as e:
        ms.errors += 1
        ms.steps_failed.append("tree")
        _log.warning("identidades.maintenance.step_failed", step="tree", error=str(e))
    _log.info(
        "identidades.maintenance.end",
        user_id=user_id,
        tree_mode=ms.tree_mode,
        targeted_items=ms.targeted.get("items"),
        tree_items=ms.tree.get("items"),
        errors=ms.errors,
    )
    return ms
