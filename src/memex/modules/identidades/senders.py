"""Resolución + persistencia del REMITENTE de un mensaje como identidad de PRIMERA CLASE (Fase 2).

El remitente es **contenido cierto** del mensaje (no una sospecha): su co-ocurrencia con lo que se
extrae del mensaje es un HECHO. Por eso se resuelve y se persiste como AVISTAMIENTO
(`mod_identidades_mentions`, `resolution_method='sender'`, `confidence=1.0`) en la EXTRACCIÓN (paso
5), uniforme entre medios — así entra a la provenance por el brazo NORMAL de menciones de
`vertex_inbox_ids` y co-ocurre solo (antes esto se DERIVABA al vuelo en
`relations/cooccurrence.py::_SENDER_PROVENANCE_SQL`, ya eliminado).

Lo dispara el ORQUESTADOR (`_process_window` → `weave_sender_structure`) para todo lote con
identidades activa, INDEPENDIENTE del ruteo LLM (el remitente es estructura del mensaje, no algo a
extraer). El workset ya filtra blacklist + gate de relevancia, así que solo corre sobre mensajes
que valen. Determinista e idempotente (no duplica menciones al re-extraer).

POLÍTICA DE CREACIÓN asimétrica por medio (resolver es uniforme; lo que cambia es a quién se crea):
- **chat (telegram):** el remitente es una PERSONA. Los chats están allowlisteados (grupos curados):
  quien escribe es gente real que vale tener en el directorio aunque el LLM nunca la extraiga. Se
  crea su persona + su identificador estable de plataforma (`platform_id`). Bots y mensajes de
  servicio (sender NULL) quedan fuera (un relay/automatización no identifica a una persona única).
- **email:** un dominio NUNCA identifica a una persona; identifica a lo sumo a UNA organización
  (keyed por el dominio → no fragmenta aunque el `from.name` varíe) y para sus usuarios es
  AFILIACIÓN. Quién es el remitente lo decide el LOCAL-PART: rol/genérico (`noreply@`/`info@`) →
  la ORG del dominio habla; individuo (`juan.perez@`) → la PERSONA por su correo + arista `afiliado`
  a la org (se preservan persona, org y la relación; NO se funde la persona en la org). El email
  exacto ya conocido gana. En **free-mail** (gmail/outlook…) el dominio no representa a nadie:
  persona por su correo si hay nombre de individuo, si no NO se crea (ruido). Default → persona.
- **social:** la CUENTA (`account`) se resuelve por handle acotado a la plataforma real; si no
  existe se crea como ORGANIZACIÓN con su handle en la plataforma real (default establecido; se
  reclasifica a persona a mano si toca). Las cuentas monitoreadas son curadas (allowlist).

Idempotencia: el alta de identidad usa `NOT EXISTS`/`ON CONFLICT DO NOTHING`; la mención usa un
`INSERT … WHERE NOT EXISTS` (no re-inserta si ya hay una mención 'sender' para ese mensaje).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.source import SourceKind
from memex.logging import get_logger
from memex.modules.identidades.module import (
    _affiliate,
    _insert_identifier,
    _resolve_fuzzy_or_create,
    load_known_index,
)
from memex.modules.identidades.normalize import (
    is_freemail,
    is_generic_localpart,
    is_role_email,
    norm_identifier,
)
from memex.modules.identidades.resolve import (
    KIND_ORG,
    KIND_PERSONA,
    KnownIdentifier,
    KnownIdentity,
    KnownIndex,
    Resolution,
)
from memex.modules.identidades.schema import IdentityItem
from memex.relations.canales import sync_canales
from memex.relations.deterministic import weave_afiliacion, weave_participa_en

_log = get_logger("memex.modules.identidades.senders")


# --- avistamiento del remitente (mención 'sender', idempotente) -------------------------- #


def _insert_sender_mention(
    conn: Connection,
    user_id: int,
    inbox_id: int,
    *,
    identity_id: int,
    resolved_kind: str,
    name: str,
    mentioned_kind: str,
    email: str | None = None,
    handle: str | None = None,
) -> bool:
    """Persiste el avistamiento del REMITENTE como mención (resolution_method='sender',
    `confidence=1.0`: el remitente es contenido cierto). Idempotente: NO inserta si ya hay una
    mención 'sender' para ese inbox (re-extracción no duplica). Devuelve True si insertó."""
    row = conn.execute(
        text(
            """
            INSERT INTO mod_identidades_mentions
              (user_id, source_inbox_ids, evidence, mentioned_name, mentioned_kind, email, handle,
               confidence, resolved_kind, resolved_identity_id, resolution_method)
            SELECT :uid, ARRAY[:mid]::bigint[], 'sender', :name, :mkind, :email, :handle,
                   1.0, :rkind, :rid, 'sender'
            WHERE NOT EXISTS (
                SELECT 1 FROM mod_identidades_mentions
                WHERE user_id = :uid AND resolution_method = 'sender'
                  AND :mid = ANY(source_inbox_ids)
            )
            RETURNING id
            """
        ),
        {
            "uid": user_id,
            "mid": inbox_id,
            "name": name,
            "mkind": mentioned_kind,
            "email": email,
            "handle": handle,
            "rkind": resolved_kind,
            "rid": identity_id,
        },
    ).first()
    return row is not None


# --- chat (telegram): persona + platform_id + canal + participa_en + mención -------------- #


def _find_unknown_senders(
    conn: Connection, user_id: int, inbox_ids: Sequence[int] | None = None
) -> list[dict[str, str | None]]:
    """Remitentes de chat humanos SIN identifier `platform_id` aún: una fila por `user_id` de
    plataforma, con el username/display_name más reciente (el título de la gente cambia). Con
    `inbox_ids` acota a los remitentes de esos mensajes (tejido por-lote)."""
    scope = "" if inbox_ids is None else " AND i.id = ANY(:ids)"
    params: dict[str, Any] = {"u": user_id}
    if inbox_ids is not None:
        params["ids"] = list(inbox_ids)
    rows = (
        conn.execute(
            text(
                f"""
                SELECT DISTINCT ON (i.payload->'sender'->>'user_id')
                       i.payload->'sender'->>'user_id'      AS tg_id,
                       i.payload->'sender'->>'username'     AS username,
                       i.payload->'sender'->>'display_name' AS display_name
                FROM inbox i
                WHERE i.user_id = :u
                  AND i.payload->'sender'->>'user_id' IS NOT NULL
                  AND (i.payload->'sender'->>'is_bot')::boolean IS NOT TRUE
                  AND NOT EXISTS (
                        SELECT 1 FROM mod_identidades_identifiers f
                        WHERE f.user_id = i.user_id AND f.platform = 'telegram'
                          AND f.kind = 'platform_id'
                          AND f.value_norm = i.payload->'sender'->>'user_id'){scope}
                ORDER BY i.payload->'sender'->>'user_id', i.id DESC
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def _identity_by_handle(conn: Connection, user_id: int, username_norm: str) -> int | None:
    """Identidad que ya tiene este username de telegram como identifier `handle`, si la hay."""
    val = conn.execute(
        text(
            """
            SELECT identity_id FROM mod_identidades_identifiers
            WHERE user_id = :u AND platform = 'telegram' AND kind = 'handle'
              AND value_norm = :vn
            ORDER BY identity_id LIMIT 1
            """
        ),
        {"u": user_id, "vn": username_norm},
    ).scalar()
    return int(val) if val is not None else None


def ensure_chat_sender_identities(
    conn: Connection, user_id: int, inbox_ids: Sequence[int] | None = None
) -> int:
    """Crea (una sola vez) la identidad `persona` de cada remitente de CHAT aún desconocido y le
    ata su identificador estable (`platform_id`). Si el username ya era identifier de una identidad
    existente, ENRIQUECE (ata el `platform_id` a esa identidad) en vez de crear. Con `inbox_ids`
    acota a los remitentes de esos mensajes (tejido por-lote); sin él, barre todo el user. Devuelve
    cuántas identidades CREÓ (el enriquecimiento no cuenta)."""
    created = 0
    enriched = 0
    for s in _find_unknown_senders(conn, user_id, inbox_ids):
        tg_id = str(s["tg_id"])
        username = s["username"]
        username_norm = norm_identifier("handle", username) if username else ""
        identity_id = _identity_by_handle(conn, user_id, username_norm) if username_norm else None
        if identity_id is None:
            display = (
                s["display_name"]
                or (f"@{username_norm}" if username_norm else "")
                or (f"telegram {tg_id}")
            )
            identity_id = int(
                conn.execute(
                    text(
                        """
                        INSERT INTO mod_identidades
                          (user_id, kind, display_name, source, interest)
                        VALUES (:u, 'persona', :n, 'extraction', FALSE)
                        RETURNING id
                        """
                    ),
                    {"u": user_id, "n": display},
                ).scalar_one()
            )
            created += 1
            if username_norm:
                _insert_identifier(
                    conn, user_id, identity_id, "telegram", "handle", str(username), username_norm
                )
        else:
            enriched += 1
        _insert_identifier(conn, user_id, identity_id, "telegram", "platform_id", tg_id, tg_id)
    if created or enriched:
        _log.info("identidades.senders.done", user_id=user_id, created=created, enriched=enriched)
    return created


def _weave_chat_sender_mentions(conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> int:
    """Persiste el avistamiento del remitente de cada mensaje de chat del lote (mención 'sender').
    Corre DESPUÉS de `ensure_chat_sender_identities` (el `platform_id` ya existe → el JOIN resuelve
    la persona). Bots y mensajes de servicio quedan fuera (no tienen `platform_id`). Idempotente.
    Devuelve cuántas menciones insertó."""
    rows = (
        conn.execute(
            text(
                """
                SELECT i.id AS mid, idf.identity_id AS iid,
                       COALESCE(i.payload->'sender'->>'display_name',
                                '@' || (i.payload->'sender'->>'username'),
                                'telegram ' || (i.payload->'sender'->>'user_id')) AS name
                FROM inbox i
                JOIN mod_identidades_identifiers idf
                  ON idf.user_id = i.user_id AND idf.platform = 'telegram'
                 AND idf.kind = 'platform_id'
                 AND idf.value_norm = i.payload->'sender'->>'user_id'
                WHERE i.user_id = :u AND i.id = ANY(:ids)
                  AND i.payload->'sender'->>'user_id' IS NOT NULL
                  AND (i.payload->'sender'->>'is_bot')::boolean IS NOT TRUE
                """
            ),
            {"u": user_id, "ids": list(inbox_ids)},
        )
        .mappings()
        .all()
    )
    n = 0
    for r in rows:
        if _insert_sender_mention(
            conn,
            user_id,
            int(r["mid"]),
            identity_id=int(r["iid"]),
            resolved_kind=KIND_PERSONA,
            name=str(r["name"]),
            mentioned_kind=KIND_PERSONA,
        ):
            n += 1
    return n


def weave_chat_structure(
    conn: Connection, user_id: int, inbox_ids: Sequence[int]
) -> tuple[int, int, int]:
    """Teje la estructura de chat de un LOTE (paso 5, al procesar la conversación), en la misma tx:
    (1) upsert del canal, (2) creación de la identidad del remitente desconocido + su `platform_id`,
    (3) arista REAL «participa_en» (remitente→canal), (4) avistamiento del remitente (mención
    'sender' por mensaje → co-ocurre con lo extraído). Orden obligatorio: canal e identifier deben
    existir antes de `participa_en` (que los JOINea) y de la mención (que JOINea el `platform_id`).
    Determinista e idempotente; independiente del ruteo LLM. Devuelve
    (canales, remitentes_creados, participa)."""
    ids = list(inbox_ids)
    if not ids:
        return 0, 0, 0
    canales = sync_canales(conn, user_id, ids)
    senders = ensure_chat_sender_identities(conn, user_id, ids)
    participa = weave_participa_en(conn, user_id, ids)
    _weave_chat_sender_mentions(conn, user_id, ids)
    return canales, senders, participa


# --- email: organización por dominio (corporativo) / persona por email (free-mail) ------- #


def _org_for_domain(
    conn: Connection, user_id: int, domain: str, display_hint: str, index: KnownIndex
) -> int:
    """Asegura la ORG de un dominio, keyed por el DOMINIO (no por el nombre): UNA org por dominio
    registrable → no fragmenta aunque el `from.name` varíe entre remitentes. Si ya hay una org con
    este identifier `domain`, esa. Si no, y el remitente da un nombre de org (habla la org), se
    dedupea contra orgs ya conocidas por ese nombre (ej. "Nequi" extraída de un cuerpo); si no hay
    nombre, se crea nombrada por el DOMINIO (nunca por una persona). Le ata el dominio. Devuelve el
    id."""
    oid = index.domain_identity(domain)
    if oid is not None:
        return oid
    dvn = norm_identifier("domain", domain)
    if display_hint:
        item = IdentityItem.model_validate(
            {"source_inbox_ids": (), "name": display_hint, "kind": "organizacion"}
        )
        res = index.resolve(item)
        if res.identity_id is None:
            res, _hint = _resolve_fuzzy_or_create(conn, user_id, item, index, source="extraction")
        org_id = res.identity_id
        assert org_id is not None  # _resolve_fuzzy_or_create siempre ata o crea
    else:
        org_id = int(
            conn.execute(
                text(
                    """
                    INSERT INTO mod_identidades (user_id, kind, display_name, source, interest)
                    VALUES (:u, 'organizacion', :n, 'extraction', FALSE)
                    RETURNING id
                    """
                ),
                {"u": user_id, "n": domain},
            ).scalar_one()
        )
    _insert_identifier(conn, user_id, org_id, "domain", "domain", domain, dvn, source="extraction")
    index.add(
        KnownIdentity(
            id=org_id,
            kind=KIND_ORG,
            display_name=display_hint or domain,
            identifiers=(KnownIdentifier("domain", "domain", dvn),),
        )
    )
    return org_id


def _person_by_email(
    conn: Connection,
    user_id: int,
    email: str,
    from_name: str,
    email_norm: str,
    index: KnownIndex,
) -> int:
    """Resuelve/crea la PERSONA dueña de un correo, keyed por el EMAIL (su identificador estable).
    Si el email ya es conocido, esa identidad. Si no, resuelve/crea por NOMBRE (reusa el dedup del
    módulo: exacto + difuso) y le ata el email para que el próximo correo resuelva exacto. El email
    es la clave estable de la persona; su dominio queda como AFILIACIÓN aparte, nunca la funde."""
    iid = index.email_identity(email_norm)
    if iid is not None:
        return iid
    name = from_name.strip() or email
    item = IdentityItem.model_validate(
        {"source_inbox_ids": (), "name": name, "kind": "persona", "email": email}
    )
    res = index.resolve(item)
    if res.identity_id is None:
        res, _hint = _resolve_fuzzy_or_create(conn, user_id, item, index, source="extraction")
    pid = res.identity_id
    assert pid is not None  # _resolve_fuzzy_or_create siempre ata o crea
    _insert_identifier(conn, user_id, pid, "email", "email", email, email_norm, source="extraction")
    index.add(
        KnownIdentity(
            id=pid,
            kind=KIND_PERSONA,
            display_name=name,
            identifiers=(KnownIdentifier("email", "email", email_norm),),
        )
    )
    return pid


def _resolve_email_sender(
    conn: Connection, user_id: int, email: str, domain: str, from_name: str, index: KnownIndex
) -> Resolution | None:
    """Resuelve el remitente de un correo (política GENERAL, domain-agnóstica). Un dominio NUNCA
    identifica a una persona: identifica a lo sumo a UNA org (keyed por el dominio) y para sus
    usuarios es AFILIACIÓN. Quién es el remitente lo decide el LOCAL-PART:
    - email exacto ya conocido → esa identidad;
    - free-mail (no representa a nadie) → persona por su correo si hay nombre, si no None;
    - dominio propio + rol/genérico (`noreply@`/`info@`/`ventas@`) → la ORG del dominio habla;
    - dominio propio + individuo (`juan.perez@`) → la PERSONA por su correo + arista `afiliado` a la
      org del dominio (se preservan persona, org y la relación; NO se funde la persona en la org).
    Default ante la duda → persona. Devuelve la `Resolution` ('sender') o None (no se crea)."""
    email_norm = norm_identifier("email", email)
    if not email_norm:
        return None
    role = is_role_email(email)
    # email exacto de una identidad conocida gana (NO si es role: un relay no es clave de persona).
    if not role:
        iid = index.email_identity(email_norm)
        if iid is not None:
            return Resolution(index.kind_of(iid), iid, "sender")
    generic = is_generic_localpart(email)
    if is_freemail(domain):
        # el dominio no representa a nadie → la persona dueña de la dirección. Se crea solo con un
        # nombre usable y un local-part de individuo (un free-mail rol/genérico/anónimo es ruido).
        if from_name.strip() and not role and not generic:
            pid = _person_by_email(conn, user_id, email, from_name, email_norm, index)
            return Resolution(KIND_PERSONA, pid, "sender")
        return None
    # dominio propio: rol/genérico → habla la ORG del dominio; individuo → PERSONA + afiliación.
    if role or generic:
        org_id = _org_for_domain(conn, user_id, domain, from_name.strip(), index)
        return Resolution(index.kind_of(org_id) or KIND_ORG, org_id, "sender")
    org_id = _org_for_domain(
        conn, user_id, domain, "", index
    )  # display = dominio, NUNCA la persona
    pid = _person_by_email(conn, user_id, email, from_name, email_norm, index)
    _affiliate(conn, user_id, pid, org_id, None, source="extraction")
    weave_afiliacion(conn, user_id, pid)
    return Resolution(KIND_PERSONA, pid, "sender")


def weave_email_senders(conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> int:
    """Resuelve y persiste el remitente de cada CORREO del lote (paso 5). Carga el `KnownIndex` una
    vez (lookup + dedup intra-lote). Devuelve cuántas menciones de remitente insertó."""
    ids = list(inbox_ids)
    if not ids:
        return 0
    rows = (
        conn.execute(
            text(
                """
                SELECT i.id AS mid,
                       lower(i.payload->'from'->>'email') AS email,
                       i.payload->'from'->>'name' AS name
                FROM inbox i
                WHERE i.user_id = :u AND i.id = ANY(:ids)
                  AND i.payload->'from'->>'email' IS NOT NULL
                """
            ),
            {"u": user_id, "ids": ids},
        )
        .mappings()
        .all()
    )
    if not rows:
        return 0
    index = load_known_index(conn, user_id)
    inserted = 0
    for r in rows:
        email = str(r["email"] or "").strip()
        if "@" not in email:
            continue
        domain = norm_identifier("domain", email)
        if not domain:
            continue
        from_name = str(r["name"] or "")
        res = _resolve_email_sender(conn, user_id, email, domain, from_name, index)
        if res is None or res.identity_id is None:
            continue
        if _insert_sender_mention(
            conn,
            user_id,
            int(r["mid"]),
            identity_id=res.identity_id,
            resolved_kind=res.kind or KIND_ORG,
            name=(from_name.strip() or domain),
            mentioned_kind=res.kind or KIND_ORG,
            email=email,
        ):
            inserted += 1
    if inserted:
        _log.info("identidades.email_senders.done", user_id=user_id, mentions=inserted)
    return inserted


# --- social: organización por handle (creada en la plataforma real) ---------------------- #


def _create_social_org(
    conn: Connection, user_id: int, platform: str, account: str, handle_norm: str, index: KnownIndex
) -> int:
    """Crea la ORG de una cuenta social desconocida con su handle en la PLATAFORMA REAL (no
    'unknown' — así el próximo post de la misma cuenta resuelve por handle). Registra el mapeo en el
    índice (dedup intra-lote). NO usa `_create_entity` (que guardaría platform='unknown'). Devuelve
    el id."""
    org_id = int(
        conn.execute(
            text(
                """
                INSERT INTO mod_identidades (user_id, kind, display_name, source, interest)
                VALUES (:u, 'organizacion', :n, 'extraction', FALSE)
                RETURNING id
                """
            ),
            {"u": user_id, "n": account},
        ).scalar_one()
    )
    _insert_identifier(
        conn, user_id, org_id, platform, "handle", account, handle_norm, source="extraction"
    )
    index.add(
        KnownIdentity(
            id=org_id,
            kind=KIND_ORG,
            display_name=account,
            identifiers=(KnownIdentifier(platform, "handle", handle_norm),),
        )
    )
    return org_id


def weave_social_senders(conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> int:
    """Resuelve y persiste el remitente (la CUENTA) de cada post social del lote (paso 5). Resuelve
    por handle acotado a la plataforma real; si la cuenta no existe, la crea como org. Devuelve
    cuántas menciones de remitente insertó."""
    ids = list(inbox_ids)
    if not ids:
        return 0
    rows = (
        conn.execute(
            text(
                """
                SELECT i.id AS mid, i.payload->>'platform' AS platform,
                       i.payload->>'account' AS account
                FROM inbox i
                WHERE i.user_id = :u AND i.id = ANY(:ids)
                  AND i.payload->>'post_id' IS NOT NULL AND i.payload->>'account' IS NOT NULL
                """
            ),
            {"u": user_id, "ids": ids},
        )
        .mappings()
        .all()
    )
    if not rows:
        return 0
    index = load_known_index(conn, user_id)
    inserted = 0
    for r in rows:
        platform = str(r["platform"] or "").strip().lower()
        account = str(r["account"] or "").strip()
        if not platform or not account:
            continue
        handle_norm = norm_identifier("handle", account)
        if not handle_norm:
            continue
        iid = index.handle_identity(platform, handle_norm)
        if iid is None:
            iid = _create_social_org(conn, user_id, platform, account, handle_norm, index)
        rkind = index.kind_of(iid) or KIND_ORG
        if _insert_sender_mention(
            conn,
            user_id,
            int(r["mid"]),
            identity_id=iid,
            resolved_kind=rkind,
            name=account,
            mentioned_kind=rkind,
            handle=account,
        ):
            inserted += 1
    if inserted:
        _log.info("identidades.social_senders.done", user_id=user_id, mentions=inserted)
    return inserted


# --- dispatcher (lo llama el orquestador para todo medio) -------------------------------- #


def weave_sender_structure(
    conn: Connection, user_id: int, inbox_ids: Sequence[int], kind: SourceKind
) -> None:
    """Resuelve + persiste el remitente de cada mensaje del lote (paso 5), uniforme entre medios con
    política de creación asimétrica (ver docstring del módulo). Lo dispara el orquestador para todo
    lote con identidades activa, INDEPENDIENTE del ruteo LLM. Determinista e idempotente; no-op para
    kinds sin remitente resoluble."""
    ids = list(inbox_ids)
    if not ids:
        return
    if kind == SourceKind.CHAT:
        weave_chat_structure(conn, user_id, ids)
    elif kind == SourceKind.EMAIL:
        weave_email_senders(conn, user_id, ids)
    elif kind == SourceKind.SOCIAL:
        weave_social_senders(conn, user_id, ids)
