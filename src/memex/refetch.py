"""Re-traer UN mensaje ya ingerido desde su fuente y refrescar su `inbox.payload`.

Para qué: cuando el parser mejora (p.ej. el cuerpo vacío por un text/plain vacío delante del real,
2026-09-04) la ingesta normal NO repara lo ya guardado: el mensaje ya existe y se salta como
duplicado. Este camino vuelve a pedir el mensaje por su identidad estable (UIDVALIDITY+UID IMAP),
lo re-parsea con el código actual y reemplaza el payload. No toca `occurred_at`, `external_id` ni
las claves de dedupe (es el MISMO mensaje) ni re-corre etapas: si el cuerpo cambió, el paso
siguiente y explícito es el reproceso (`POST /inbox/{id}/reprocess`).

Hoy solo fuentes `imap`: las push del cliente local no se pueden pedir desde el server.
Superficies: `POST /inbox/{id}/refetch` y `memex-inspect refetch <ids> | --empty`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import text

from memex import sources as source_registry
from memex.core.source import SourceRecord
from memex.db import connection
from memex.logging import get_logger
from memex.sources.resolver import build_resolved_env

_log = get_logger("memex.refetch")

#: (uidvalidity, uid) → el record re-parseado, o None si el mensaje ya no está en la cuenta.
Fetcher = Callable[[int, int], SourceRecord | None]


class RefetchError(Exception):
    """Fallo esperable del refetch; `code` distingue la causa (el router lo mapea a HTTP)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImapRef:
    server: str
    uidvalidity: int
    uid: int


@dataclass(frozen=True)
class RefetchResult:
    inbox_id: int
    external_id: str
    body_before: int
    body_after: int
    body_source: str | None
    changed: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_imap_external_id(external_id: str) -> ImapRef:
    """`imap:<server>:<uidvalidity>:<uid>` — el formato que arma `parser.parse_email_message`."""
    parts = external_id.split(":")
    if len(parts) != 4 or parts[0] != "imap":
        raise RefetchError("bad_external_id", f"external_id no es IMAP: {external_id!r}")
    try:
        return ImapRef(parts[1], int(parts[2]), int(parts[3]))
    except ValueError as e:
        raise RefetchError(
            "bad_external_id", f"external_id IMAP malformado: {external_id!r}"
        ) from e


def _imap_fetcher(
    user_id: int, source_type: str, cfg: dict[str, Any], account_id: int | None
) -> Fetcher:
    """Fetcher real: resuelve los secretos del vault, instancia la `ImapSource` y pide por UID."""
    from memex.ingestors.imap.source import ImapSource

    with connection() as conn:
        env = build_resolved_env(
            conn, user_id=user_id, source_type=source_type, cfg=cfg, account_id=account_id
        )
    source = source_registry.resolve(source_type)(cfg, env=env)
    if not isinstance(source, ImapSource):
        raise RefetchError("unsupported", f"la fuente tipo {source_type!r} no soporta refetch")
    return source.fetch_one


def refetch_inbox(user_id: int, inbox_id: int, *, fetcher: Fetcher | None = None) -> RefetchResult:
    """Vuelve a traer el mensaje `inbox_id` y reemplaza su payload. `fetcher` inyectable (tests)."""
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT i.external_id, i.payload, s.type, s.config, s.account_id
                    FROM inbox i JOIN sources s ON s.id = i.source_id
                    WHERE i.id = :id AND i.user_id = :uid
                    """
                ),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        raise RefetchError("not_found", f"inbox #{inbox_id} no existe")
    source_type = str(row["type"])
    if source_type != "imap":
        raise RefetchError(
            "unsupported", f"la fuente tipo {source_type!r} no soporta refetch (solo imap)"
        )
    external_id = str(row["external_id"])
    ref = parse_imap_external_id(external_id)
    before = dict(row["payload"] or {})
    body_before = len(str(before.get("body_text") or ""))

    fetch = fetcher or _imap_fetcher(
        user_id, source_type, dict(row["config"] or {}), row["account_id"]
    )
    try:
        record = fetch(ref.uidvalidity, ref.uid)
    except RefetchError:
        raise
    except Exception as e:  # IMAP/OAuth/red: falla de la fuente, no un 500 opaco
        raise RefetchError("source_error", f"la fuente falló al re-traer: {e}") from e
    if record is None:
        raise RefetchError(
            "gone", f"UID {ref.uid} no está en la cuenta (¿borrado o cambió UIDVALIDITY?)"
        )
    if record.external_id != external_id:  # defensa: el UID debe seguir siendo ESTE mensaje
        raise RefetchError(
            "mismatch", f"la fuente devolvió {record.external_id!r} para {external_id!r}"
        )

    after = record.payload
    body_after = len(str(after.get("body_text") or ""))
    changed = after != before
    with connection() as conn:
        conn.execute(
            text("UPDATE inbox SET payload = CAST(:p AS JSONB) WHERE id = :id AND user_id = :uid"),
            {"p": json.dumps(after), "id": inbox_id, "uid": user_id},
        )
    _log.info(
        "inbox.refetched",
        inbox_id=inbox_id,
        external_id=external_id,
        body_before=body_before,
        body_after=body_after,
        changed=changed,
    )
    return RefetchResult(
        inbox_id=inbox_id,
        external_id=external_id,
        body_before=body_before,
        body_after=body_after,
        body_source=after.get("body_source"),
        changed=changed,
    )
