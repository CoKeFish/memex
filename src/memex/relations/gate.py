"""Compuerta determinista alias-aware: reemplaza las citas del resolver viejo.

El experimento de metodologías midió 0 alucinaciones validando que cada vértice que el LLM confirma
EXISTA en lo enviado. Acá se endurece esa idea: un par confirmado por el LLM solo se acepta si AMBOS
extremos APARECEN en el cuerpo del mensaje, por su nombre o por un alias. Reproducir un nombre corto
es robusto donde reproducir una cita textual alucina; y "GoogleInc" en el cuerpo valida el vértice
"Google" (substring sobre texto normalizado — `unaccent+lower+colapso ws`, espejo de `memex_norm`),
que es justo lo que se quiere.

Formas por vértice:
- IDENTIDAD (`slug` empieza con `identidades:`): `display_name` + `aliases[]` + los `value_norm` de
  `mod_identidades_identifiers` (emails/handles/dominios — ya normalizados en la DB).
- NO-IDENTIDAD (finance/calendar/bienestar/...): el `label` proyectado del vértice.

Substring puro (NO límite de palabra: rompería el caso "GoogleInc"→"google"), con un piso de
longitud (`MIN_FORM_LEN`) para que formas de 1-2 chars no validen por ruido. Un vértice cuyas formas
son todas más cortas que el piso no se puede validar → su par no se confirma (sesgo a precisión).
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.modules.identidades.normalize import normalize_match
from memex.relations.edges import Ref
from memex.relations.vertices import Vertex

#: Piso de longitud (sobre la forma normalizada) para considerar una forma como evidencia. Formas
#: más cortas (iniciales, tickers de 1-2 letras) validarían por ruido de substring.
MIN_FORM_LEN = 3

_IDENTITY_PREFIX = "identidades:"


def normalize_body(rendered: str) -> str:
    """El texto del mensaje normalizado igual que las formas (base de la comparación)."""
    return normalize_match(rendered)


def _identity_forms(conn: Connection, user_id: int, ident_ids: list[int]) -> dict[int, set[str]]:
    """Formas de superficie por identidad: nombre + alias (normalizados) + identificadores
    (`value_norm`, ya normalizados). Keyed por id de identidad."""
    out: dict[int, set[str]] = {i: set() for i in ident_ids}
    if not ident_ids:
        return out
    for r in conn.execute(
        text(
            "SELECT id, display_name, aliases FROM mod_identidades "
            "WHERE user_id = :u AND id = ANY(:ids)"
        ),
        {"u": user_id, "ids": ident_ids},
    ).mappings():
        forms = out[int(r["id"])]
        forms.add(normalize_match(str(r["display_name"])))
        for a in r["aliases"] or []:
            forms.add(normalize_match(str(a)))
    for r in conn.execute(
        text(
            "SELECT identity_id, value_norm FROM mod_identidades_identifiers "
            "WHERE identity_id = ANY(:ids)"
        ),
        {"ids": ident_ids},
    ).mappings():
        # value_norm ya viene normalizado por `norm_identifier`; igual lo pasamos por colapso ws.
        out[int(r["identity_id"])].add(normalize_match(str(r["value_norm"])))
    return out


def vertex_surface_forms(
    conn: Connection, user_id: int, refs: Iterable[Ref], vmap: dict[Ref, Vertex]
) -> dict[Ref, frozenset[str]]:
    """Las formas (normalizadas, ≥ `MIN_FORM_LEN`) con que cada vértice puede aparecer en el texto.
    Identidades: nombre + alias + identificadores; el resto: su `label`."""
    refs = list(dict.fromkeys(refs))
    ident_ids = sorted({r.id for r in refs if r.slug.startswith(_IDENTITY_PREFIX)})
    by_ident = _identity_forms(conn, user_id, ident_ids)
    out: dict[Ref, frozenset[str]] = {}
    for ref in refs:
        if ref.slug.startswith(_IDENTITY_PREFIX):
            raw = by_ident.get(ref.id, set())
        else:
            v = vmap.get(ref)
            raw = {normalize_match(v.label)} if v is not None else set()
        out[ref] = frozenset(f for f in raw if len(f) >= MIN_FORM_LEN)
    return out


def appears(forms: frozenset[str], body_norm: str) -> bool:
    """¿Alguna forma del vértice aparece (substring) en el cuerpo normalizado?"""
    return any(f in body_norm for f in forms)


def both_endpoints_present(
    src_forms: frozenset[str], dst_forms: frozenset[str], body_norm: str
) -> bool:
    """La compuerta: AMBOS extremos del par deben aparecer en el cuerpo por nombre/alias."""
    return appears(src_forms, body_norm) and appears(dst_forms, body_norm)
