"""Salud de los VÉRTICES del directorio de identidades (read-only).

El módulo es dueño de la salud de sus vértices (contrato de módulos de dominio). El dedup
determinista (`KnownIndex` + trigram) atrapa lo fácil al escribir; esta superficie detecta lo
SOSPECHOSO que queda por debajo y conviene revisar (merge, set-kind, set-parent, unmerge):

- huérfanas: `extraction` con 0 menciones y sin hijos/afiliación/contraparte → candidatas a borrar;
- identificador COMPARTIDO: el mismo email/handle/dominio en >1 identidad (la resolución se vuelve
  ambigua: el `KnownIndex` resuelve a una sola) → candidatas a fusionar;
- dominio compartido: caso especial (kind='domain') — dos orgs reclaman el mismo dominio;
- homónimo CROSS-KIND: mismo `name_norm` en kinds distintos (el match exacto es kind-scoped, no se
  funden ni se proponen solos) → revisar si son la misma entidad mal-tipada;
- near-dup por CONTENCIÓN: un nombre es subconjunto estricto del otro (mismo kind) y el trigram lo
  pierde ('Jose David' ⊂ 'Jose David Reyes');
- `org_core` vacío: org/producto cuyo núcleo quedó vacío → indeduplicable por difuso;
- ciclo de jerarquía: un nodo que es su propio ancestro (el CHECK de la DB solo ve el self-loop).

NO muta nada: arma un `HealthReport` para que el dueño/agente decida. Lo consume la CLI
(`memex identidad health`) y puede engancharse al ciclo del scheduler como diagnóstico.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: Tope de filas por categoría (a escala no se vuelcan miles; lista una muestra accionable).
_LIMIT = 200


@dataclass(frozen=True)
class Entity:
    """Una identidad sospechosa (huérfana / org_core vacío)."""

    id: int
    kind: str
    display_name: str


@dataclass(frozen=True)
class SharedValue:
    """Un valor (identificador o nombre) reclamado por VARIAS identidades."""

    key: str  # value_norm del identificador, o name_norm
    detail: str  # 'platform:kind' del identificador, o los kinds en colisión
    ids: tuple[int, ...]


@dataclass(frozen=True)
class Pair:
    """Un par de identidades del MISMO kind sospechoso de ser el mismo (contención de tokens)."""

    a_id: int
    b_id: int
    kind: str
    a_name: str
    b_name: str


@dataclass
class HealthReport:
    """Diagnóstico read-only del directorio de un user."""

    total: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    orphans: list[Entity] = field(default_factory=list)
    shared_identifiers: list[SharedValue] = field(default_factory=list)
    cross_kind_homonyms: list[SharedValue] = field(default_factory=list)
    containment_dups: list[Pair] = field(default_factory=list)
    empty_org_core: list[Entity] = field(default_factory=list)
    cycles: list[int] = field(default_factory=list)

    @property
    def suspicious(self) -> int:
        """Total de hallazgos accionables (todas las categorías menos los conteos)."""
        return (
            len(self.orphans)
            + len(self.shared_identifiers)
            + len(self.cross_kind_homonyms)
            + len(self.containment_dups)
            + len(self.empty_org_core)
            + len(self.cycles)
        )


def _counts(conn: Connection, user_id: int) -> tuple[int, dict[str, int]]:
    rows = conn.execute(
        text("SELECT kind, count(*) AS n FROM mod_identidades WHERE user_id = :u GROUP BY kind"),
        {"u": user_id},
    ).all()
    by_kind = {str(r[0]): int(r[1]) for r in rows}
    return sum(by_kind.values()), by_kind


def _orphans(conn: Connection, user_id: int) -> list[Entity]:
    """`extraction` sin menciones y sin hijos / afiliación / contraparte de finanzas (no aporta a
    nada): residuo típico de un reproceso con deriva de nombre (la vieja perdió su mención)."""
    rows = conn.execute(
        text(
            """
            SELECT i.id, i.kind, i.display_name
            FROM mod_identidades i
            WHERE i.user_id = :u AND i.source = 'extraction'
              AND NOT EXISTS (SELECT 1 FROM mod_identidades_mentions m
                              WHERE m.resolved_identity_id = i.id)
              AND NOT EXISTS (SELECT 1 FROM mod_identidades c WHERE c.parent_identity_id = i.id)
              AND NOT EXISTS (SELECT 1 FROM mod_identidades_person_orgs po
                              WHERE po.person_id = i.id OR po.org_id = i.id)
              AND NOT EXISTS (SELECT 1 FROM mod_finance_consolidated f
                              WHERE f.counterparty_identity_id = i.id)
            ORDER BY i.id
            LIMIT :lim
            """
        ),
        {"u": user_id, "lim": _LIMIT},
    ).all()
    return [Entity(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def _shared_identifiers(conn: Connection, user_id: int) -> list[SharedValue]:
    """El mismo (platform, kind, value_norm) en >1 identidad: la resolución por ese identificador es
    ambigua (el índice resuelve a una sola). Excluye `platform_id` (clave legítima)."""
    rows = conn.execute(
        text(
            """
            SELECT platform, kind, value_norm, array_agg(DISTINCT identity_id ORDER BY identity_id)
            FROM mod_identidades_identifiers
            WHERE user_id = :u AND kind <> 'platform_id'
            GROUP BY platform, kind, value_norm
            HAVING count(DISTINCT identity_id) > 1
            ORDER BY count(DISTINCT identity_id) DESC
            LIMIT :lim
            """
        ),
        {"u": user_id, "lim": _LIMIT},
    ).all()
    return [SharedValue(str(r[2]), f"{r[0]}:{r[1]}", tuple(int(x) for x in r[3])) for r in rows]


def _cross_kind_homonyms(conn: Connection, user_id: int) -> list[SharedValue]:
    """Mismo `name_norm` en kinds distintos: persona↔org/producto no se funden ni se proponen solos
    (el match exacto es kind-scoped). Revisar si es la misma entidad mal-tipada."""
    rows = conn.execute(
        text(
            """
            SELECT name_norm, array_agg(id ORDER BY id), array_agg(DISTINCT kind ORDER BY kind)
            FROM mod_identidades
            WHERE user_id = :u AND coalesce(name_norm, '') <> ''
            GROUP BY name_norm
            HAVING count(DISTINCT kind) > 1
            ORDER BY name_norm
            LIMIT :lim
            """
        ),
        {"u": user_id, "lim": _LIMIT},
    ).all()
    return [
        SharedValue(str(r[0]), ",".join(str(k) for k in r[2]), tuple(int(x) for x in r[1]))
        for r in rows
    ]


def _containment_dups(conn: Connection, user_id: int) -> list[Pair]:
    """Pares del MISMO kind donde un `name_norm` está contenido ESTRICTAMENTE en el otro (lado corto
    >= 2 tokens): subcadenas/abreviaciones que el trigram pierde (espeja
    `find_containment_candidates` como barrido global)."""
    rows = conn.execute(
        text(
            """
            SELECT a.id, b.id, a.kind, a.display_name, b.display_name
            FROM mod_identidades a
            JOIN mod_identidades b
              ON a.user_id = b.user_id AND a.kind = b.kind AND a.id < b.id
            WHERE a.user_id = :u
              AND string_to_array(b.name_norm, ' ') @> string_to_array(a.name_norm, ' ')
              AND cardinality(string_to_array(a.name_norm, ' ')) >= 2
              AND cardinality(string_to_array(b.name_norm, ' '))
                  > cardinality(string_to_array(a.name_norm, ' '))
            ORDER BY a.id, b.id
            LIMIT :lim
            """
        ),
        {"u": user_id, "lim": _LIMIT},
    ).all()
    return [Pair(int(r[0]), int(r[1]), str(r[2]), str(r[3]), str(r[4])) for r in rows]


def _empty_org_core(conn: Connection, user_id: int) -> list[Entity]:
    rows = conn.execute(
        text(
            """
            SELECT id, kind, display_name FROM mod_identidades
            WHERE user_id = :u AND kind IN ('organizacion', 'producto')
              AND coalesce(org_core, '') = ''
            ORDER BY id LIMIT :lim
            """
        ),
        {"u": user_id, "lim": _LIMIT},
    ).all()
    return [Entity(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def _cycles(conn: Connection, user_id: int) -> list[int]:
    """Nodos que son su propio ancestro (ciclo de jerarquía). El CHECK de la DB solo atrapa el
    self-loop directo; este barrido recursivo (cota de profundidad) encuentra los multinivel."""
    rows = conn.execute(
        text(
            """
            WITH RECURSIVE up AS (
                SELECT id, parent_identity_id, ARRAY[id] AS path, FALSE AS cyc
                FROM mod_identidades
                WHERE user_id = :u AND parent_identity_id IS NOT NULL
                UNION ALL
                SELECT u.id, p.parent_identity_id, u.path || p.id, p.id = ANY(u.path)
                FROM up u
                JOIN mod_identidades p ON p.id = u.parent_identity_id AND p.user_id = :u
                WHERE NOT u.cyc AND cardinality(u.path) < 50
            )
            SELECT DISTINCT id FROM up WHERE cyc ORDER BY id LIMIT :lim
            """
        ),
        {"u": user_id, "lim": _LIMIT},
    ).all()
    return [int(r[0]) for r in rows]


def vertex_health(conn: Connection, user_id: int) -> HealthReport:
    """Diagnóstico read-only completo del directorio del user. No muta nada."""
    total, by_kind = _counts(conn, user_id)
    return HealthReport(
        total=total,
        by_kind=by_kind,
        orphans=_orphans(conn, user_id),
        shared_identifiers=_shared_identifiers(conn, user_id),
        cross_kind_homonyms=_cross_kind_homonyms(conn, user_id),
        containment_dups=_containment_dups(conn, user_id),
        empty_org_core=_empty_org_core(conn, user_id),
        cycles=_cycles(conn, user_id),
    )
