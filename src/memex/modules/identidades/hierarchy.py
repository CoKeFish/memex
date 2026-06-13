"""Organizador LLM de la PERTENENCIA («sub») de identidades.

Sistema genérico de jerarquía: una organización o un producto puede colgar de otra entrada
(«pertenece a») — programa→universidad, producto→empresa, filial→matriz, área→organización. A
diferencia del dedup (par-por-par, `dedup_llm.py`), esto es HOLÍSTICO: una sola llamada con la
lista COMPLETA de orgs + productos del user (los productos van marcados `[producto]`); el LLM
devuelve la jerarquía en JSON y se aplica DIRECTO, sin cola de confirmación manual (el dueño puede
corregir un padre a mano por la UI). Los hijos con padre decidido a mano (`parent_source` 'manual'
por la UI o 'agent' por `memex identidad set-parent`) están PINEADOS: el organizador los salta, así
una corrida periódica no pisa esas correcciones. Los padres creados por nombre son SIEMPRE
organizaciones (el padre inferido de un producto es su empresa). Mold: `calendar/merge_llm.py`.

Salida del LLM = `{"links": [{"child_id", "parent_id"|"parent_name", "cleaned_name"?}]}`:
- `child_id` siempre es un id existente; el padre por `parent_id` (id existente) XOR `parent_name`
  (padre que debería existir y no está en la lista → se resuelve por `org_core` o se crea con
  `source='extraction'`).
- Parseo ULTRA-DEFENSIVO: se descarta toda entrada con ids fuera de la lista, XOR violado o child
  repetido (sin tumbar la corrida).
- Al aplicar: se saltan self-parent y los que crearían CICLO (multinivel, chequeado en Python; el
  CHECK de la DB solo atrapa el self-loop directo). Idempotente (setear el padre +
  crear-por-org_core no duplican). El rename `cleaned_name` está APAGADO por default (cambia
  columnas generadas).

Registra UNA `llm_calls` (`purpose="identidades_hierarchy"`). Cliente LLM inyectable (fake en test).
`LLMQuotaError` se propaga (el scheduler la captura; es una sola llamada, sin best-effort por ítem).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import CostAccum, record_llm_call
from memex.db import connection
from memex.llm import ChatMessage, LLMClient, LLMResult, aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.modules.identidades.normalize import org_core
from memex.modules.identidades.prompt import IDENTIDADES_HIERARCHY_SYSTEM_PROMPT

_log = get_logger("memex.modules.identidades.hierarchy")

_DEFAULT_LIMIT = 500
#: La salida lista un link por org sub; escala con el nº de orgs (cota holgada para un directorio).
_MAX_TOKENS = 4096


@dataclass(frozen=True)
class OrgView:
    """Vista mínima de una entrada colgable (org o producto) para mostrarle al LLM
    (id interno + nombre + alias + kind; los productos se marcan en la lista)."""

    id: int
    display_name: str
    aliases: tuple[str, ...]
    kind: str = "organizacion"


@dataclass(frozen=True)
class HierarchyLink:
    """Un vínculo propuesto: `child_id` pertenece a un padre (por id existente XOR por nombre)."""

    child_id: int
    parent_id: int | None
    parent_name: str | None
    cleaned_name: str | None


@dataclass
class OrganizeStats:
    """Resumen de una corrida del organizador de pertenencia."""

    orgs: int = 0  # entradas consideradas (organizaciones + productos)
    linked: int = 0  # parent_identity_id seteados
    created: int = 0  # padres nuevos creados por nombre
    cleaned: int = 0  # display_name limpiados (solo si apply_cleanup)
    skipped: int = 0  # links descartados al aplicar (self-parent / ciclo)
    errors: int = 0
    #: Costo LLM acumulado (identidades es source-less → el total se atribuye por `purpose`).
    cost: CostAccum = field(default_factory=CostAccum)


# --- ciclo (compartido con el PATCH del API) --------------------------------------- #


def would_create_cycle(conn: Connection, user_id: int, child_id: int, parent_id: int) -> bool:
    """True si colgar `child_id` de `parent_id` crearía un ciclo (es decir, `parent_id` ya desciende
    de `child_id`). Sube la cadena de padres desde `parent_id`; si llega a `child_id`, hay ciclo. El
    CHECK de la DB solo atrapa el self-loop directo, así que el anti-ciclo multinivel vive acá."""
    if parent_id == child_id:
        return True
    cur: int | None = parent_id
    seen: set[int] = set()
    while cur is not None:
        if cur == child_id:
            return True
        if cur in seen:  # defensivo: ciclo preexistente (no debería ocurrir)
            break
        seen.add(cur)
        row = conn.execute(
            text("SELECT parent_identity_id FROM mod_identidades WHERE id = :id AND user_id = :u"),
            {"id": cur, "u": user_id},
        ).scalar()
        cur = int(row) if row is not None else None
    return False


# --- parseo de la respuesta del LLM ------------------------------------------------ #


def _parse_links(content: str, valid_ids: set[int]) -> list[HierarchyLink]:
    """Parsea `{"links":[...]}`. ULTRA-DEFENSIVO: basura → `[]`; descarta entradas con child/parent
    fuera de `valid_ids`, XOR de padre violado o `child_id` repetido (la primera gana)."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("links")
    if not isinstance(raw, list):
        return []
    out: list[HierarchyLink] = []
    seen_children: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid = item.get("child_id")
        if not isinstance(cid, int) or isinstance(cid, bool) or cid not in valid_ids:
            continue
        if cid in seen_children:
            continue
        pid = item.get("parent_id")
        has_pid = isinstance(pid, int) and not isinstance(pid, bool) and pid in valid_ids
        pname_raw = item.get("parent_name")
        pname = pname_raw.strip() if isinstance(pname_raw, str) else ""
        has_pname = pname != ""
        if has_pid == has_pname:  # XOR: exactamente uno (ni ambos ni ninguno)
            continue
        clean_raw = item.get("cleaned_name")
        cleaned = clean_raw.strip() if isinstance(clean_raw, str) else ""
        seen_children.add(cid)
        out.append(
            HierarchyLink(
                child_id=cid,
                parent_id=pid if has_pid else None,
                parent_name=pname if has_pname else None,
                cleaned_name=cleaned or None,
            )
        )
    return out


def _serialize_orgs(orgs: list[OrgView]) -> str:
    lines = []
    for o in orgs:
        alias = f" — {', '.join(o.aliases)}" if o.aliases else ""
        tag = " [producto]" if o.kind == "producto" else ""
        lines.append(f"{o.id}: {o.display_name}{alias}{tag}")
    return "\n".join(lines)


async def organize(llm: LLMClient, orgs: list[OrgView]) -> tuple[list[HierarchyLink], LLMResult]:
    """Le pide al LLM la jerarquía de pertenencia de toda la lista. Devuelve los links válidos
    (parseados/filtrados contra los ids reales) + el LLMResult (para el costo)."""
    valid_ids = {o.id for o in orgs}
    user_content = "ORGANIZACIONES Y PRODUCTOS (id: nombre — alias):\n" + _serialize_orgs(orgs)
    result = await llm.complete(
        [
            ChatMessage("system", IDENTIDADES_HIERARCHY_SYSTEM_PROMPT),
            ChatMessage("user", user_content),
        ],
        response_format="json_object",
        temperature=0.0,
        max_tokens=_MAX_TOKENS,
    )
    return _parse_links(result.content, valid_ids), result


# --- aplicación -------------------------------------------------------------------- #


def _load_orgs(conn: Connection, user_id: int, limit: int) -> list[OrgView]:
    """Las entradas COLGABLES del directorio: organizaciones y productos (producto→empresa)."""
    rows = (
        conn.execute(
            text(
                """
                SELECT id, display_name, aliases, kind
                FROM mod_identidades
                WHERE user_id = :u AND kind IN ('organizacion','producto')
                ORDER BY id
                LIMIT :lim
                """
            ),
            {"u": user_id, "lim": limit},
        )
        .mappings()
        .all()
    )
    if len(rows) >= limit:
        _log.warning("identidades.hierarchy.truncated", user_id=user_id, limit=limit)
    return [
        OrgView(
            id=int(r["id"]),
            display_name=str(r["display_name"]),
            aliases=tuple(r["aliases"] or ()),
            kind=str(r["kind"]),
        )
        for r in rows
    ]


def _resolve_or_create_parent(conn: Connection, user_id: int, name: str) -> tuple[int, bool]:
    """Resuelve `name` a una org O producto existente por `org_core` (si el padre nombrado ya vive
    como producto, se reusa — no se duplica como org); si no hay, la crea como ORGANIZACIÓN
    (source='extraction', `metadata.created_by='hierarchy_llm'`). Devuelve `(parent_id, created)`.
    Idempotente: dos links al mismo padre-por-nombre convergen a la misma entrada."""
    core = org_core(name)
    if core:
        existing = conn.execute(
            text(
                "SELECT id FROM mod_identidades "
                "WHERE user_id = :u AND kind IN ('organizacion','producto') AND org_core = :core "
                "ORDER BY id LIMIT 1"
            ),
            {"u": user_id, "core": core},
        ).scalar()
        if existing is not None:
            return int(existing), False
    new_id = conn.execute(
        text(
            """
            INSERT INTO mod_identidades (user_id, kind, display_name, source, metadata)
            VALUES (:u, 'organizacion', :name, 'extraction',
                    jsonb_build_object('created_by', 'hierarchy_llm'))
            RETURNING id
            """
        ),
        {"u": user_id, "name": name},
    ).scalar_one()
    return int(new_id), True


def _set_parent(conn: Connection, user_id: int, child_id: int, parent_id: int) -> None:
    conn.execute(
        text(
            """
            UPDATE mod_identidades
            SET parent_identity_id = :p,
                metadata = jsonb_set(metadata, '{parent_source}', to_jsonb(CAST('llm' AS TEXT))),
                updated_at = NOW()
            WHERE id = :c AND user_id = :u
            """
        ),
        {"p": parent_id, "c": child_id, "u": user_id},
    )


def _apply_cleanup_name(conn: Connection, user_id: int, child_id: int, cleaned_name: str) -> bool:
    """Renombra el sub a `cleaned_name` y deja el nombre completo original como alias. Devuelve True
    si cambió. APAGADO por default (cambia las columnas generadas name_norm/org_core)."""
    cur = conn.execute(
        text("SELECT display_name FROM mod_identidades WHERE id = :c AND user_id = :u"),
        {"c": child_id, "u": user_id},
    ).scalar()
    if cur is None or str(cur) == cleaned_name:
        return False
    conn.execute(
        text(
            """
            UPDATE mod_identidades SET
              aliases = (
                SELECT COALESCE(array_agg(DISTINCT x), '{}')
                FROM unnest(aliases || ARRAY[display_name]) AS x WHERE x <> :new
              ),
              display_name = :new,
              updated_at = NOW()
            WHERE id = :c AND user_id = :u
            """
        ),
        {"new": cleaned_name, "c": child_id, "u": user_id},
    )
    return True


def _pinned_children(conn: Connection, user_id: int) -> set[int]:
    """Ids cuyo padre lo decidió el dueño (UI, `parent_source='manual'`) o el agente
    (`'agent'`): el organizador LLM NO los pisa, ni para cambiar el padre ni para re-linkear
    uno quitado a mano."""
    rows = conn.execute(
        text(
            "SELECT id FROM mod_identidades "
            "WHERE user_id = :u AND metadata->>'parent_source' IN ('manual','agent')"
        ),
        {"u": user_id},
    ).all()
    return {int(r[0]) for r in rows}


def _apply_links(
    conn: Connection,
    user_id: int,
    links: list[HierarchyLink],
    *,
    apply_cleanup: bool,
) -> tuple[int, int, int, int]:
    """Aplica los links sobre `conn` (atómico con la tx). Devuelve `(linked, created, cleaned,
    skipped)`. Cada link: salta los hijos pineados (padre manual/agent), resuelve/crea el padre,
    salta self-parent y ciclos, setea el padre."""
    linked = created = cleaned = skipped = 0
    pinned = _pinned_children(conn, user_id)
    for link in links:
        # ANTES de resolver el padre: un hijo pineado no debe ni crear el padre-por-nombre.
        if link.child_id in pinned:
            skipped += 1
            _log.info("identidades.hierarchy.skip_pinned", child_id=link.child_id)
            continue
        if link.parent_id is not None:
            parent_id = link.parent_id
        elif link.parent_name is not None:
            parent_id, was_created = _resolve_or_create_parent(conn, user_id, link.parent_name)
            if was_created:
                created += 1
        else:  # no debería pasar (XOR garantizado en el parseo)
            skipped += 1
            continue
        if parent_id == link.child_id:
            skipped += 1
            _log.info("identidades.hierarchy.skip_self", child_id=link.child_id)
            continue
        if would_create_cycle(conn, user_id, link.child_id, parent_id):
            skipped += 1
            _log.info(
                "identidades.hierarchy.skip_cycle", child_id=link.child_id, parent_id=parent_id
            )
            continue
        _set_parent(conn, user_id, link.child_id, parent_id)
        linked += 1
        if (
            apply_cleanup
            and link.cleaned_name
            and _apply_cleanup_name(conn, user_id, link.child_id, link.cleaned_name)
        ):
            cleaned += 1
    return linked, created, cleaned, skipped


async def run_organize(
    user_id: int,
    *,
    limit: int = _DEFAULT_LIMIT,
    apply_cleanup: bool = False,
    client: LLMClient | None = None,
) -> OrganizeStats:
    """Organiza la jerarquía de pertenencia del user con UNA llamada al LLM y la aplica directo.
    Idempotente (re-correr re-evalúa). `client` inyectable (tests con fake). `LLMQuotaError` se
    propaga (es una sola llamada; el scheduler la captura)."""
    stats = OrganizeStats()
    with connection() as conn:
        orgs = _load_orgs(conn, user_id, limit)
    stats.orgs = len(orgs)
    if not orgs:
        _log.info("identidades.hierarchy.empty", user_id=user_id)
        return stats

    owns_client = client is None
    llm: LLMClient = client or build_llm_client("identidades_hierarchy", user_id=user_id)
    _log.info("identidades.hierarchy.start", user_id=user_id, orgs=len(orgs))
    try:
        links, result = await organize(llm, orgs)
        with connection() as conn:
            linked, created, cleaned, skipped = _apply_links(
                conn, user_id, links, apply_cleanup=apply_cleanup
            )
        stats.linked = linked
        stats.created = created
        stats.cleaned = cleaned
        stats.skipped = skipped
        record_llm_call(
            user_id=user_id,
            purpose="identidades_hierarchy",
            model=result.model,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            cache_hit_tokens=result.usage.cache_hit_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            status="ok",
            # Sin source: la lista combina orgs de muchos mensajes → se identifica por purpose.
            source_id=None,
            metadata={"orgs": len(orgs), "links": len(links), "linked": linked, "created": created},
        )
        stats.cost.calls += 1
        stats.cost.prompt_tokens += result.usage.prompt_tokens
        stats.cost.completion_tokens += result.usage.completion_tokens
        stats.cost.cost_usd += result.cost_usd
    finally:
        if owns_client:
            await aclose_llm(llm)

    _log.info(
        "identidades.hierarchy.end",
        user_id=user_id,
        orgs=stats.orgs,
        linked=stats.linked,
        created=stats.created,
        cleaned=stats.cleaned,
        skipped=stats.skipped,
        llm_calls=stats.cost.calls,
        llm_cost_usd=str(stats.cost.cost_usd),
    )
    return stats
