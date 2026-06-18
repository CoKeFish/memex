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

import tldextract

#: Extractor de la Public Suffix List EMBEBIDA y OFFLINE: `suffix_list_urls=()` desactiva el fetch
#: por HTTP (usa el snapshot que trae el paquete) y `cache_dir=None` evita escribir caché en disco →
#: determinista, sin red. Carga perezosa del snapshot en la 1ª llamada. Lo usa `registrable_domain`.
_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

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

#: Tokens de local-part role/relay: la dirección NO identifica a una persona/entidad única.
_ROLE_TOKENS = frozenset(
    {
        "notification",
        "notifications",
        "notify",
        "noreply",
        "donotreply",
        "mailer",
        "daemon",
        "postmaster",
        "bounce",
        "bounces",
    }
)

#: Dominios de correo PERSONAL gratuito (free-mail). El dominio NO representa a una organización: el
#: remitente es la PERSONA dueña de la dirección, no el proveedor. Por eso, al resolver el remitente
#: de un correo (Fase 2), un dominio free-mail NO crea la org del dominio (sería ruido como una org
#: "gmail.com") — se resuelve por el email exacto si ya se conoce. Lista CURADA (no exhaustiva): se
#: amplía si aparece un proveedor frecuente. Cubre los comunes globales + los usados en Colombia.
FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "outlook.es",
        "hotmail.com",
        "hotmail.es",
        "hotmail.co.uk",
        "live.com",
        "live.com.mx",
        "msn.com",
        "yahoo.com",
        "yahoo.es",
        "yahoo.com.mx",
        "ymail.com",
        "rocketmail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "protonmail.com",
        "proton.me",
        "pm.me",
        "gmx.com",
        "gmx.net",
        "zoho.com",
        "mail.com",
        "yandex.com",
        "tutanota.com",
        "fastmail.com",
        "hey.com",
    }
)


def is_freemail(domain: str) -> bool:
    """True si `domain` es un proveedor de correo personal gratuito (gmail, outlook, …).

    El dominio de un free-mail NO identifica a una organización (lo comparten millones de personas
    sin relación entre sí): el remitente es la persona dueña de la dirección. Por eso la resolución
    del remitente de correo NO crea una org para estos dominios. `domain` se compara normalizado
    (lower, ya sin el local-part); pasar `norm_identifier('domain', email)` o el dominio pelado."""
    return domain.strip().lower() in FREEMAIL_DOMAINS


def is_role_email(email: str) -> bool:
    """True si `email` es una dirección ROLE/RELAY (noreply, notifications, mailer-daemon, …).

    Estas direcciones NO identifican a una persona/entidad única: las comparte mucha gente (el relay
    `notifications@github.com` reenvía a nombre de muchos usuarios distintos; `*-noreply@linkedin`
    igual). Por eso NO se usan como clave de identidad: una mención con un email role se resuelve
    por NOMBRE, no por email (si no, fusionaría remitentes distintos)."""
    local = email.split("@", 1)[0].lower()
    flat = re.sub(r"[^a-z]", "", local)  # no-reply / no.reply / no_reply → noreply
    if "noreply" in flat or "donotreply" in flat:
        return True
    return any(tok in _ROLE_TOKENS for tok in re.split(r"[._+-]", local))


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


def registrable_domain(host: str) -> str:
    """Dominio REGISTRABLE (eTLD+1) de un host: lo que la organización realmente posee, colapsando
    los subdominios. `mail.acme.com`/`billing.acme.com` → `acme.com`; `accounts.google.com` →
    `google.com`. Usa la Public Suffix List (vía `_TLD_EXTRACT`, offline) para acertar con los
    sufijos multi-etiqueta: `a.tienda.com.co` → `tienda.com.co`, `forums.bbc.co.uk` → `bbc.co.uk`
    (un recorte ingenuo de 2 etiquetas mezclaría orgs distintas bajo `com.co`/`co.uk`).

    Fallback al host pelado (lower) si la PSL no determina un sufijo (host interno, IP, sin TLD
    público) — no perder el dato."""
    h = host.strip().lower()
    if not h:
        return ""
    return _TLD_EXTRACT(h).top_domain_under_public_suffix or h


#: Dominios que IGNORAN los puntos del local-part (Gmail y su alias googlemail).
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def _norm_email(value: str) -> str:
    """Canonicaliza un email para el match: lower; quita el sub-addressing `+tag` (RFC 5233, misma
    casilla) de TODO dominio; en Gmail/Googlemail ignora además los puntos del local-part y unifica
    el dominio a `gmail.com`. Así `j.doe+promo@gmail.com` y `jdoe@gmail.com` colapsan al mismo."""
    local, sep, domain = value.lower().partition("@")
    if not sep:
        return value.lower()
    local = local.split("+", 1)[0]  # +tag (sub-addressing) → misma casilla
    if domain in _GMAIL_DOMAINS:
        local = local.replace(".", "")  # Gmail ignora los puntos del local-part
        domain = "gmail.com"  # googlemail.com == gmail.com
    return f"{local}@{domain}"


def _norm_phone(value: str) -> str:
    """Normaliza un teléfono: solo dígitos y `+`. Best-effort a E.164 con país por defecto Colombia
    (+57) para un MÓVIL local de 10 dígitos que empieza en 3 (3XX XXX XXXX); ya en `+` se respeta.
    Otros formatos quedan como los dígitos (no se adivina prefijo para no mis-prefijar un fijo)."""
    digits = re.sub(r"[^0-9+]", "", value)
    if not digits or digits.startswith("+"):
        return digits
    if len(digits) == 10 and digits.startswith("3"):
        return "+57" + digits
    return digits


def norm_identifier(kind: str, value: str) -> str:
    """Normaliza el valor de un identificador para el match acotado por plataforma. ESPEJO de la
    normalización usada en el sync/extracción al insertar `mod_identidades_identifiers.value_norm`.

    - email: lower + canonicalización (quita `+tag`; en Gmail/Googlemail ignora puntos y unifica
      el dominio) → dos grafías de la misma casilla colapsan. Ver `_norm_email`.
    - phone: solo dígitos y `+`, best-effort a E.164 (+57 móvil colombiano). Ver `_norm_phone`.
    - handle: lower + strip + sin `@` inicial.
    - domain: parte tras el último `@` (si la hay) → DOMINIO REGISTRABLE (eTLD+1, colapsa
      subdominios; ver `registrable_domain`). Idempotente sobre un registrable.
    - url: lower + strip + sin `/` final.
    - platform_id: strip tal cual (el id que asigna la plataforma es opaco; sin lower-tricks).
    - otro: lower + strip.
    """
    v = value.strip()
    if kind == "platform_id":
        return v
    if kind == "email":
        return _norm_email(v)
    if kind == "phone":
        return _norm_phone(v)
    if kind == "handle":
        return v.lower().lstrip("@")
    if kind == "domain":
        return registrable_domain(v.rpartition("@")[2])
    if kind == "url":
        return v.lower().rstrip("/")
    return v.lower()
