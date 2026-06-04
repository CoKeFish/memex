"""Normalización de identidades — ESPEJO Python de las funciones SQL de la migración 0033.

La DB normaliza para los índices/trigram (columnas generadas `name_norm`/`org_core` vía
`memex_norm`/`memex_org_core`). Python replica SOLO para el match EXACTO en memoria (`KnownIndex`)
y para computar `value_norm` de los identificadores al insertarlos. La paridad Python↔SQL se
verifica en `tests/identidades/test_normalize.py`.

- `normalize_match` ↔ `memex_norm` (SQL): unaccent + lower + colapso de whitespace. Divergencia
  conocida y aceptada en letras especiales NO descomponibles por NFKD que `unaccent` SÍ mapea
  (ß→ss, ø→o, æ→ae, …): irrelevante para español/inglés; el trigram (DB) igual las acerca.
- `org_core` ↔ `memex_org_core` (SQL): `normalize_match` + quitar puntos + puntuación→espacio +
  strip de sufijos legales (`_ORG_SUFFIXES`) + colapso. `_ORG_SUFFIXES` DEBE coincidir con el de la
  migración 0033 (test de paridad lo verifica).
- `norm_identifier`: normaliza el valor de un identificador según su `kind` (email/phone/handle/
  domain/url) para el match acotado por plataforma.
"""

from __future__ import annotations

import re
import unicodedata

#: Sufijos legales/societarios a quitar del núcleo de orgs. ESPEJO de
#: `migrations/versions/0033_identidades_v2.py::_ORG_SUFFIXES` (mantener en sync — test de paridad).
_ORG_SUFFIXES: tuple[str, ...] = (
    "incorporated",
    "corporation",
    "technologies",
    "holdings",
    "company",
    "limited",
    "holding",
    "ltda",
    "grupo",
    "group",
    "gmbh",
    "corp",
    "oyj",
    "sapi",
    "eirl",
    "inc",
    "llc",
    "llp",
    "plc",
    "ltd",
    "sas",
    "sac",
    "sca",
    "scs",
    "spa",
    "slu",
    "srl",
    "pty",
    "pte",
    "ohg",
    "co",
    "sa",
    "sl",
    "ag",
    "bv",
    "oy",
    "kk",
    "kg",
)

_ORG_SUFFIX_RE = re.compile(r"\b(?:" + "|".join(_ORG_SUFFIXES) + r")\b")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _strip_accents(text: str) -> str:
    """Quita diacríticos por descomposición NFKD (≈ `unaccent` de Postgres para latín acentuado)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_match(text: str) -> str:
    """unaccent + lower + colapso de whitespace. Espejo de `memex_norm` (SQL). Clave del match
    EXACTO en memoria (nombre/alias) y base de `org_core`."""
    return _WS_RE.sub(" ", _strip_accents(text).lower()).strip()


def org_core(name: str) -> str:
    """Núcleo de una organización para el match difuso: `normalize_match` + quitar puntos +
    puntuación→espacio + strip de sufijos legales. Espejo de `memex_org_core` (SQL).

    Ej.: 'Acme S.A.S.' → 'acme'; 'Unity Technologies' → 'unity'; 'Grupo Bolívar S.A.' → 'bolivar'.
    """
    base = normalize_match(name).replace(".", "")
    base = _NON_ALNUM_RE.sub(" ", base)
    base = _ORG_SUFFIX_RE.sub("", base)
    return _WS_RE.sub(" ", base).strip()


def norm_identifier(kind: str, value: str) -> str:
    """Normaliza el valor de un identificador para el match acotado por plataforma. ESPEJO de la
    normalización usada en el sync/extracción al insertar `mod_identidades_identifiers.value_norm`.

    - email: lower + strip.
    - phone: solo dígitos y `+`.
    - handle: lower + strip + sin `@` inicial.
    - domain: parte tras el último `@` (si la hay), lower + strip.
    - url: lower + strip + sin `/` final.
    - otro: lower + strip.
    """
    v = value.strip()
    if kind == "phone":
        return re.sub(r"[^0-9+]", "", v)
    if kind == "handle":
        return v.lower().lstrip("@")
    if kind == "domain":
        return v.rpartition("@")[2].strip().lower()
    if kind == "url":
        return v.lower().rstrip("/")
    return v.lower()
