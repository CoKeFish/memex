"""Worker de sincronización de contactos externos (slice 1 = ingress read-only idempotente).

`memex-identidades sync` trae el delta de contactos de un proveedor (Google People) y los escribe
DIRECTO a `mod_identidades_persons` (las personas del proveedor ya vienen estructuradas; NO pasan
por inbox/classifier/LLM — el sync vive dentro del módulo, calca `calendar.sync`).

Idempotencia: un `provider_resource_name` no se duplica entre corridas (UNIQUE parcial + upsert por
SELECT-then-INSERT/UPDATE comparando `etag`). El cursor incremental (`sync_token` de la People API)
se persiste en la cuenta; el próximo `sync` solo trae el delta. 410 GONE (token caducado) → full
resync transparente. Los borrados llegan en el delta (`deleted=True`) y se marcan SUAVE
(`metadata.deleted`) sin borrar la fila (coexistencia / auditoría, calca calendar).

Si un contacto trae organización (`org_name`), se asegura una fila en `mod_identidades_orgs`
(`source='google_contacts'`, `interest=FALSE`: descubierta, no curada) + la asociación persona↔org.

Token OAuth: resuelto desde el VAULT de la cuenta del dashboard (Decisión 6), no de disco. Cliente
del proveedor inyectable (tests con fake, sin red), igual que el worker de calendar.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.logging import get_logger
from memex.modules.identidades.providers import oauth, resolve
from memex.modules.identidades.providers.base import (
    ContactsProvider,
    ContactsProviderError,
    ContactsSyncTokenExpired,
    ProviderContact,
)
from memex.modules.identidades.providers.config import ContactsSyncConfig
from memex.modules.identidades.providers.google import GooglePeopleClient

_log = get_logger("memex.modules.identidades.sync")


@dataclass
class ContactsSyncStats:
    """Resumen de una corrida de sync (espeja los contadores de `mod_identidades_sync_runs`)."""

    pulled: int = 0  # contactos traídos del proveedor (incluye borrados)
    created: int = 0  # filas nuevas insertadas
    modified: int = 0  # filas existentes actualizadas (cambió el etag)
    deleted: int = 0  # contactos marcados deleted en el proveedor
    unchanged: int = 0  # contactos sin cambios (mismo etag)
    errors: int = 0


@dataclass(frozen=True)
class _Account:
    id: int  # id de la fila mod_identidades_provider_accounts
    user_id: int
    provider: str
    account_id: int | None  # FK a accounts (dashboard) cuyo vault tiene el token
    sync_token: str | None
    enabled: bool


# --- DB helpers -------------------------------------------------------------------- #


def _load_account(conn: Connection, user_id: int, account_id: int) -> _Account | None:
    row = conn.execute(
        text(
            """
            SELECT id, user_id, provider, account_id, sync_token, enabled
            FROM mod_identidades_provider_accounts
            WHERE id = :id AND user_id = :uid
            """
        ),
        {"id": account_id, "uid": user_id},
    ).first()
    if row is None:
        return None
    return _Account(
        id=int(row[0]),
        user_id=int(row[1]),
        provider=str(row[2]),
        account_id=(int(row[3]) if row[3] is not None else None),
        sync_token=(str(row[4]) if row[4] is not None else None),
        enabled=bool(row[5]),
    )


def _start_run(conn: Connection, user_id: int, account_id: int) -> int:
    return int(
        conn.execute(
            text(
                """
                INSERT INTO mod_identidades_sync_runs (user_id, provider_account_id)
                VALUES (:uid, :aid)
                RETURNING id
                """
            ),
            {"uid": user_id, "aid": account_id},
        ).scalar_one()
    )


def _finish_run(conn: Connection, run_id: int, stats: ContactsSyncStats, *, status: str) -> None:
    conn.execute(
        text(
            """
            UPDATE mod_identidades_sync_runs SET
              pulled = :pulled, created = :created, modified = :modified, deleted = :deleted,
              unchanged = :unchanged, errors = :errors, status = :status, finished_at = NOW()
            WHERE id = :id
            """
        ),
        {
            "id": run_id,
            "pulled": stats.pulled,
            "created": stats.created,
            "modified": stats.modified,
            "deleted": stats.deleted,
            "unchanged": stats.unchanged,
            "errors": stats.errors,
            "status": status,
        },
    )


def _ensure_org(conn: Connection, user_id: int, name: str) -> int:
    """Asegura una org descubierta desde un contacto (idempotente por nombre case-insensitive).
    `interest=FALSE` (descubierta, no de la lista curada). Devuelve el id."""
    existing = conn.execute(
        text(
            "SELECT id FROM mod_identidades_orgs WHERE user_id = :uid AND lower(name) = lower(:n)"
        ),
        {"uid": user_id, "n": name},
    ).first()
    if existing is not None:
        return int(existing[0])
    return int(
        conn.execute(
            text(
                """
                INSERT INTO mod_identidades_orgs (user_id, name, kind, interest, source)
                VALUES (:uid, :n, 'organizacion', FALSE, 'google_contacts')
                RETURNING id
                """
            ),
            {"uid": user_id, "n": name},
        ).scalar_one()
    )


def _ensure_person_org(
    conn: Connection, user_id: int, person_id: int, org_id: int, role: str | None
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id, role, source)
            VALUES (:uid, :pid, :oid, :role, 'google_contacts')
            ON CONFLICT (person_id, org_id) DO NOTHING
            """
        ),
        {"uid": user_id, "pid": person_id, "oid": org_id, "role": role},
    )


def _link_org(
    conn: Connection, account: _Account, person_id: int, contact: ProviderContact
) -> None:
    if not contact.org_name:
        return
    org_id = _ensure_org(conn, account.user_id, contact.org_name)
    _ensure_person_org(conn, account.user_id, person_id, org_id, contact.role)


def _upsert_person(conn: Connection, account: _Account, contact: ProviderContact) -> str:
    """Inserta o actualiza una persona por su `provider_resource_name`. Devuelve la acción
    ('created'/'modified'/'unchanged'). Asegura org + asociación si el contacto trae org."""
    existing = conn.execute(
        text(
            """
            SELECT id, provider_etag FROM mod_identidades_persons
            WHERE provider = :provider AND provider_account_id = :aid
              AND provider_resource_name = :rn
            """
        ),
        {"provider": account.provider, "aid": account.id, "rn": contact.resource_name},
    ).first()

    params: dict[str, object] = {
        "uid": account.user_id,
        "display_name": contact.display_name,
        "given_name": contact.given_name,
        "family_name": contact.family_name,
        "emails": list(contact.emails),
        "phones": list(contact.phones),
        "org_name": contact.org_name,
        "role": contact.role,
        "provider": account.provider,
        "aid": account.id,
        "rn": contact.resource_name,
        "etag": contact.etag,
        "photo_url": contact.photo_url,
    }

    if existing is None:
        person_id = int(
            conn.execute(
                text(
                    """
                    INSERT INTO mod_identidades_persons
                      (user_id, display_name, given_name, family_name, emails, phones, org_name,
                       role, source, interest, provider, provider_account_id,
                       provider_resource_name, provider_etag, photo_url)
                    VALUES
                      (:uid, :display_name, :given_name, :family_name, :emails, :phones, :org_name,
                       :role, 'google_contacts', TRUE, :provider, :aid, :rn, :etag, :photo_url)
                    RETURNING id
                    """
                ),
                params,
            ).scalar_one()
        )
        _link_org(conn, account, person_id, contact)
        return "created"

    person_id = int(existing[0])
    current_etag = existing[1]
    if contact.etag is not None and current_etag == contact.etag:
        return "unchanged"

    conn.execute(
        text(
            """
            UPDATE mod_identidades_persons SET
              display_name = :display_name, given_name = :given_name, family_name = :family_name,
              emails = :emails, phones = :phones, org_name = :org_name, role = :role,
              provider_etag = :etag, photo_url = :photo_url, updated_at = NOW()
            WHERE id = :id
            """
        ),
        {**params, "id": person_id},
    )
    _link_org(conn, account, person_id, contact)
    return "modified"


def _mark_deleted(conn: Connection, account: _Account, resource_name: str) -> bool:
    """Marca SUAVE (`metadata.deleted=true`) la persona borrada en el proveedor. NO borra la fila
    (coexistencia / auditoría). Devuelve True si existía una fila para marcar."""
    row = conn.execute(
        text(
            """
            UPDATE mod_identidades_persons
            SET metadata = metadata || '{"deleted": true}'::jsonb, updated_at = NOW()
            WHERE provider = :provider AND provider_account_id = :aid
              AND provider_resource_name = :rn
            RETURNING id
            """
        ),
        {"provider": account.provider, "aid": account.id, "rn": resource_name},
    ).first()
    return row is not None


def _save_cursor(conn: Connection, account_id: int, sync_token: str | None) -> None:
    if sync_token is not None:
        conn.execute(
            text(
                "UPDATE mod_identidades_provider_accounts "
                "SET sync_token = :t, last_sync_at = NOW() WHERE id = :id"
            ),
            {"t": sync_token, "id": account_id},
        )
    else:
        conn.execute(
            text(
                "UPDATE mod_identidades_provider_accounts SET last_sync_at = NOW() WHERE id = :id"
            ),
            {"id": account_id},
        )


# --- fetch (red) ------------------------------------------------------------------- #


async def _fetch_all(
    client: ContactsProvider, *, sync_token: str | None, full: bool
) -> tuple[list[ProviderContact], str | None]:
    """Pagina `list_delta` hasta agotar, acumulando contactos y capturando el `next_sync_token`
    final. Si el `sync_token` caducó (410) hace un full resync una vez."""
    use_token = None if full else sync_token
    contacts: list[ProviderContact] = []
    page_token: str | None = None
    new_sync_token: str | None = None

    while True:
        try:
            page = await client.list_delta(sync_token=use_token, page_token=page_token)
        except ContactsSyncTokenExpired:
            if use_token is None:  # ya era full sync → no hay token que invalidar; propagá
                raise
            _log.warning("identidades.sync.token_expired_full_resync")
            use_token = None
            page_token = None
            contacts = []
            continue
        contacts.extend(page.contacts)
        if page.next_page_token:
            page_token = page.next_page_token
            continue
        new_sync_token = page.next_sync_token
        break

    return contacts, new_sync_token


# --- entry point ------------------------------------------------------------------- #


async def run_sync(
    user_id: int,
    account_id: int,
    *,
    full: bool = False,
    client: ContactsProvider | None = None,
) -> ContactsSyncStats:
    """Sincroniza (ingress) una cuenta de contactos externa hacia `mod_identidades_persons`.

    `full=True` ignora el cursor y trae todo. `client` inyectable (tests con fake, sin red).
    Best-effort: errores de proveedor se loguean + registran en `mod_identidades_sync_runs`.
    """
    stats = ContactsSyncStats()

    with connection() as conn:
        account = _load_account(conn, user_id, account_id)
    if account is None:
        _log.error("identidades.sync.account_not_found", user_id=user_id, account_id=account_id)
        stats.errors += 1
        return stats
    if not account.enabled:
        _log.info("identidades.sync.account_disabled", account_id=account_id)
        return stats

    owns_client = client is None
    active: ContactsProvider
    if client is not None:
        active = client
    else:
        if account.account_id is None:
            _log.error("identidades.sync.no_vault_account", account_id=account_id)
            stats.errors += 1
            return stats
        with connection() as conn:
            access = oauth.access_token(account.provider, conn=conn, account_id=account.account_id)
        active = resolve(account.provider)(ContactsSyncConfig.from_env(), access)

    _log.info(
        "identidades.sync.start",
        user_id=user_id,
        account_id=account_id,
        provider=account.provider,
        full=full,
    )

    try:
        contacts, new_sync_token = await _fetch_all(
            active, sync_token=account.sync_token, full=full
        )
    except ContactsProviderError as e:
        stats.errors += 1
        _log.error(
            "identidades.sync.fetch_failed",
            account_id=account_id,
            status=e.status_code,
            msg=str(e),
        )
        with connection() as conn:
            run_id = _start_run(conn, user_id, account_id)
            _finish_run(conn, run_id, stats, status="error")
        return stats
    finally:
        if owns_client and isinstance(active, GooglePeopleClient):
            await active.aclose()

    stats.pulled = len(contacts)
    with connection() as conn:
        run_id = _start_run(conn, user_id, account_id)
        for contact in contacts:
            if contact.deleted:
                if _mark_deleted(conn, account, contact.resource_name):
                    stats.deleted += 1
                continue
            action = _upsert_person(conn, account, contact)
            if action == "created":
                stats.created += 1
            elif action == "modified":
                stats.modified += 1
            else:
                stats.unchanged += 1
        _save_cursor(conn, account_id, new_sync_token)
        _finish_run(conn, run_id, stats, status="ok")

    _log.info(
        "identidades.sync.end",
        user_id=user_id,
        account_id=account_id,
        pulled=stats.pulled,
        created=stats.created,
        modified=stats.modified,
        deleted=stats.deleted,
        unchanged=stats.unchanged,
    )
    return stats
