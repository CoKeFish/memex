"""`IdentidadesModule` — extractor de identidades (personas y organizaciones) al directorio.

Satisface `InterestModule` estructuralmente. Por cada identidad detectada en un mensaje:
  - si DEDUPLICA contra una identidad que ya está en el directorio (por email/dominio/handle/nombre/
    alias) → le suma el mensaje como evidencia (no la duplica);
  - si es nueva → la CREA en el directorio en estado `interest=FALSE` (source='extraction', la
    "Detectada"); el usuario después la promueve a interés.

Cada avistamiento queda en `mod_identidades_mentions` (la evidencia: qué mensaje nombró a quién),
SIEMPRE ligado a una identidad. El dedup es determinista (sin LLM). Declara `provide_domain`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.source import HealthResult, SourceKind
from memex.logging import get_logger
from memex.modules.contract import CAP_EXTRACT, CAP_PROVIDE_DOMAIN, ExtractionItem, ModuleContext
from memex.modules.dedup import forget_inbox_rows
from memex.modules.identidades.prompt import IDENTIDADES_SYSTEM_PROMPT
from memex.modules.identidades.resolve import KnownIndex, KnownOrg, KnownPerson, Resolution
from memex.modules.identidades.schema import IdentityItem

_log = get_logger("memex.modules.identidades")

#: kinds que viven en `mod_identidades_orgs` (el resto — persona/unknown — va a personas).
_ORG_KINDS = frozenset({"organizacion", "producto", "agente"})


class IdentidadesModule:
    """Extrae identidades al directorio (`mod_identidades_persons`/`_orgs`) + su evidencia."""

    slug: ClassVar[str] = "identidades"
    interest: ClassVar[str] = (
        "Personas y organizaciones/productos mencionados: contactos, empresas, marcas, "
        "herramientas, agentes (IA) con los que la persona interactúa o sobre los que habla. "
        "NO la propia persona ni publicidad genérica."
    )
    extraction_schema: ClassVar[type[ExtractionItem]] = IdentityItem
    extraction_prompt: ClassVar[str] = IDENTIDADES_SYSTEM_PROMPT
    capabilities: ClassVar[frozenset[str]] = frozenset({CAP_EXTRACT, CAP_PROVIDE_DOMAIN})
    consumes_kinds: ClassVar[frozenset[SourceKind]] = frozenset(
        {SourceKind.EMAIL, SourceKind.CHAT, SourceKind.SOCIAL}
    )
    depends_on: ClassVar[tuple[str, ...]] = ()
    #: `()` = dedup por MECANISMO PROPIO: `KnownIndex.resolve` (email→dominio→handle→nombre→alias) +
    #: SELECT-first + UNIQUE(user_id, lower(name)) en orgs. La identidad es multi-señal, no una
    #: clave simple.
    identity_fields: ClassVar[tuple[str, ...]] = ()

    async def persist(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Entrypoint del orquestador; delega la unicidad a `self.dedup`."""
        return await self.dedup(ctx, items)

    async def dedup(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Mecanismo propio (`()` en `identity_fields`): por cada mención dedup contra el directorio
        (`KnownIndex.resolve`); si es nueva la crea (no-interés); registra el avistamiento como
        evidencia. Todo en `ctx.conn` (atómico con el cursor)."""
        mentions = [i for i in items if isinstance(i, IdentityItem)]
        if not mentions:
            return 0
        index = load_known_index(ctx.conn, ctx.user_id)
        for m in mentions:
            res = index.resolve(m)
            if res.kind is None:  # nueva identidad → entra al directorio en no-interés
                res = _create_entity(ctx.conn, ctx.user_id, m, index)
            _insert_mention(ctx.conn, ctx.user_id, m, res)
        return len(mentions)

    async def health_check(self) -> HealthResult:
        return HealthResult(
            status="healthy", detail="identidades module ready", checked_at=datetime.now(UTC)
        )

    def read_for_inbox(
        self, conn: Connection, user_id: int, inbox_ids: Sequence[int]
    ) -> list[dict[str, Any]]:
        """Menciones públicas atribuidas a `inbox_ids` (quién se nombró + cómo resolvió). NO expone
        ids internos de resolución (`resolved_person_id`/`resolved_org_id`) ni el directorio."""
        rows = (
            conn.execute(
                text(
                    """
                    SELECT mentioned_name, mentioned_kind, evidence, resolved_kind,
                           resolution_method
                    FROM mod_identidades_mentions
                    WHERE user_id = :uid AND CAST(:ids AS BIGINT[]) && source_inbox_ids
                    ORDER BY id
                    """
                ),
                {"uid": user_id, "ids": list(inbox_ids)},
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows]

    def forget_inbox(self, conn: Connection, user_id: int, inbox_ids: Sequence[int]) -> int:
        """Olvida lo aportado por `inbox_ids`: saca la referencia y borra la mención solo si queda
        huérfana. NO toca el directorio (personas/orgs), que trasciende al mensaje."""
        return forget_inbox_rows(
            conn, "mod_identidades_mentions", user_id=user_id, inbox_ids=inbox_ids
        )


def _create_entity(
    conn: Connection, user_id: int, item: IdentityItem, index: KnownIndex
) -> Resolution:
    """Crea una identidad NUEVA en el directorio (source='extraction', interest=FALSE) y la registra
    en el índice para que el resto de la corrida deduplique contra ella."""
    if item.kind in _ORG_KINDS:
        # SELECT-first por si el nombre ya existe con otra grafía (evita chocar el UNIQUE).
        row = conn.execute(
            text("SELECT id FROM mod_identidades_orgs WHERE user_id=:u AND lower(name)=lower(:n)"),
            {"u": user_id, "n": item.name},
        ).first()
        org_id = (
            int(row[0])
            if row is not None
            else int(
                conn.execute(
                    text(
                        """
                        INSERT INTO mod_identidades_orgs (user_id, name, kind, interest, source)
                        VALUES (:u, :n, :k, FALSE, 'extraction')
                        RETURNING id
                        """
                    ),
                    {"u": user_id, "n": item.name, "k": item.kind},
                ).scalar_one()
            )
        )
        index.add_org(KnownOrg(id=org_id, name=item.name))
        return Resolution("org", None, org_id, "created")

    handles = {"social": item.handle} if item.handle else {}
    emails = [item.email] if item.email else []
    person_id = int(
        conn.execute(
            text(
                """
                INSERT INTO mod_identidades_persons
                  (user_id, display_name, emails, handles, source, interest)
                VALUES (:u, :n, :em, CAST(:h AS JSONB), 'extraction', FALSE)
                RETURNING id
                """
            ),
            {"u": user_id, "n": item.name, "em": emails, "h": json.dumps(handles)},
        ).scalar_one()
    )
    index.add_person(
        KnownPerson(
            id=person_id,
            display_name=item.name,
            emails=emails,
            handles=[item.handle] if item.handle else [],
        )
    )
    return Resolution("person", person_id, None, "created")


def _insert_mention(conn: Connection, user_id: int, item: IdentityItem, res: Resolution) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mod_identidades_mentions
              (user_id, source_inbox_ids, evidence, mentioned_name, mentioned_kind, email, handle,
               org_hint, role_hint, confidence, resolved_kind, resolved_person_id, resolved_org_id,
               resolution_method)
            VALUES (:uid, :ids, :evidence, :name, :kind, :email, :handle, :org, :role, :confidence,
                    :rkind, :rperson, :rorg, :method)
            """
        ),
        {
            "uid": user_id,
            "ids": list(item.source_inbox_ids),
            "evidence": item.evidence,
            "name": item.name,
            "kind": item.kind,
            "email": item.email,
            "handle": item.handle,
            "org": item.org,
            "role": item.role,
            "confidence": item.confidence,
            "rkind": res.kind,
            "rperson": res.person_id,
            "rorg": res.org_id,
            "method": res.method,
        },
    )


def _handle_values(handles: object) -> list[str]:
    """`handles` es JSONB ({plataforma: handle}); para la resolución usamos los VALORES."""
    if isinstance(handles, dict):
        return [str(v) for v in handles.values() if v]
    return []


def load_known_index(conn: Connection, user_id: int) -> KnownIndex:
    """Arma el `KnownIndex` del user desde `mod_identidades_persons` / `mod_identidades_orgs`.
    Compartido por `persist` (extracción) y el handle `provide_domain` (`domain.py`)."""
    persons = [
        KnownPerson(
            id=int(r["id"]),
            display_name=str(r["display_name"]),
            emails=tuple(r["emails"] or ()),
            handles=tuple(_handle_values(r["handles"])),
        )
        for r in conn.execute(
            text(
                "SELECT id, display_name, emails, handles FROM mod_identidades_persons "
                "WHERE user_id = :uid"
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    ]
    orgs = [
        KnownOrg(
            id=int(r["id"]),
            name=str(r["name"]),
            aliases=tuple(r["aliases"] or ()),
            domains=tuple(r["domains"] or ()),
        )
        for r in conn.execute(
            text(
                "SELECT id, name, aliases, domains FROM mod_identidades_orgs WHERE user_id = :uid"
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    ]
    return KnownIndex(persons, orgs)
