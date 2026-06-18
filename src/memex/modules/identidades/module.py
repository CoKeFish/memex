"""`IdentidadesModule` — extractor de identidades (personas, organizaciones y productos) al
directorio.

Satisface `InterestModule` estructuralmente. Por cada identidad detectada en un mensaje, `dedup`
la resuelve contra el directorio (`mod_identidades`) por SEÑALES FUERTES deterministas (incluido el
REMITENTE del mensaje) y, si no hay match exacto, por SIMILITUD DE TRIGRAMAS:

  - similitud ≥ `HIGH_THRESHOLD` → AUTO-MERGE: ata a la identidad existente y suma el nombre
    variante como ALIAS (para que el próximo match sea exacto);
  - zona gris `[LOW, HIGH)` → crea la identidad provisional (no-interés) y encola un CANDIDATO de
    merge (`mod_identidades_merge_candidates`) que el desempate LLM (`dedup_llm`) resuelve después;
  - < `LOW_THRESHOLD` → identidad NUEVA (no-interés, source='extraction').

Cada avistamiento queda en `mod_identidades_mentions` (la evidencia: qué mensaje nombró a quién),
SIEMPRE ligado a una identidad (`resolved_identity_id`). El dedup inline es DETERMINISTA (sin LLM);
el desempate LLM corre en una fase aparte. Declara `provide_domain`.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.source import HealthResult, SourceKind
from memex.logging import get_logger
from memex.modules.contract import (
    CAP_EXTRACT,
    CAP_PROVIDE_DOMAIN,
    ExtractionItem,
    ModuleContext,
)
from memex.modules.dedup import forget_inbox_rows
from memex.modules.identidades.domain import IdentidadesDomainReader
from memex.modules.identidades.fuzzy import (
    HIGH_THRESHOLD,
    LOW_THRESHOLD,
    find_containment_candidates,
    find_fuzzy_candidates,
)
from memex.modules.identidades.normalize import is_role_email, norm_identifier
from memex.modules.identidades.prompt import IDENTIDADES_SYSTEM_PROMPT
from memex.modules.identidades.resolve import (
    KIND_DESCONOCIDO,
    KIND_ORG,
    KIND_PERSONA,
    KIND_PRODUCTO,
    KnownIdentifier,
    KnownIdentity,
    KnownIndex,
    Resolution,
)
from memex.modules.identidades.schema import IdentityItem
from memex.relations.deterministic import weave_afiliacion, weave_event

_log = get_logger("memex.modules.identidades")

#: kind de la mención → kind canónico del directorio. 'unknown' (el escape del extractor) y
#: cualquier valor fuera del mapa caen a DESCONOCIDO («pendiente de clasificación»): el extractor no
#: afirmó un tipo → no se adivina persona, se deja que un sistema lo defina después.
_IDENTITY_KIND_BY_MENTION = {
    "persona": KIND_PERSONA,
    "organizacion": KIND_ORG,
    "producto": KIND_PRODUCTO,
}


def _identity_kind(mention_kind: str) -> str:
    """Mapea el `kind` de la mención al `kind` canónico (persona|organizacion|producto); lo no
    afirmado por el extractor ('unknown' o fuera de mapa) → `desconocido` (no se adivina)."""
    return _IDENTITY_KIND_BY_MENTION.get(mention_kind, KIND_DESCONOCIDO)


@dataclass(frozen=True)
class _MergeHint:
    """Vecino difuso para la traza: contra quién comparó, su score, y si auto-mergeó (≥HIGH) o quedó
    como candidato en zona gris (→ desempate LLM)."""

    other_id: int
    score: float
    auto_merge: bool


class IdentidadesModule:
    """Extrae identidades al directorio (`mod_identidades`) + su evidencia (`_mentions`)."""

    slug: ClassVar[str] = "identidades"
    interest: ClassVar[str] = (
        "Personas, organizaciones y productos mencionados: contactos, empresas, instituciones, "
        "marcas, apps, herramientas, IAs con los que la persona interactúa o sobre los que habla. "
        "NO la propia persona ni publicidad genérica."
    )
    extraction_schema: ClassVar[type[ExtractionItem]] = IdentityItem
    extraction_prompt: ClassVar[str] = IDENTIDADES_SYSTEM_PROMPT
    capabilities: ClassVar[frozenset[str]] = frozenset({CAP_EXTRACT, CAP_PROVIDE_DOMAIN})
    consumes_kinds: ClassVar[frozenset[SourceKind]] = frozenset(
        {SourceKind.EMAIL, SourceKind.CHAT, SourceKind.SOCIAL}
    )
    depends_on: ClassVar[tuple[str, ...]] = ()
    optional_deps: ClassVar[tuple[str, ...]] = ()
    #: `()` = dedup por MECANISMO PROPIO: señales fuertes (`KnownIndex`) + difuso (`pg_trgm`) +
    #: auto-merge/candidato. La identidad es multi-señal, no una clave simple.
    identity_fields: ClassVar[tuple[str, ...]] = ()

    async def persist(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Entrypoint del orquestador; delega la unicidad a `self.dedup`."""
        return await self.dedup(ctx, items)

    async def dedup(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Mecanismo propio (`()` en `identity_fields`): por cada mención resuelve contra el
        directorio (señales fuertes + difuso); auto-mergea, encola candidato o crea. Registra el
        avistamiento como evidencia. Todo en `ctx.conn` (atómico con el cursor).

        Emite `identidades.dedup.done` (agregado por unidad: breakdown por camino de resolución +
        `merge_pending`, la cola que el desempate LLM —scheduler/CLI, NO el lote— resuelve después).
        El detalle por mención vive en el tracer visual (`_trace_mention`), no en log_events."""
        mentions = [i for i in items if isinstance(i, IdentityItem)]
        if not mentions:
            return 0
        t0 = time.monotonic()
        counts = {"strong": 0, "auto_merge": 0, "gray": 0, "created": 0}
        index = load_known_index(ctx.conn, ctx.user_id)
        for m in mentions:
            try:
                res = index.resolve(m)
                hint: _MergeHint | None = None
                if res.kind is None:
                    res, hint = _resolve_fuzzy_or_create(ctx.conn, ctx.user_id, m, index)
                _insert_mention(ctx.conn, ctx.user_id, m, res)
            except Exception as e:
                # Se RE-LANZA: la tx del módulo rollbackea y la ventana cae a
                # extract.window.failed como siempre; acá solo queda el por-qué con la mención.
                _log.error(
                    "identidades.dedup.mention_failed",
                    name=m.name,
                    kind=m.kind,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                    exc_info=True,
                )
                raise
            self._trace_mention(ctx, m, res, hint)
            # Mismo orden de ramas que _trace_mention: creada / auto-merge / zona gris / fuerte.
            if res.method == "created":
                counts["created"] += 1
            elif hint is not None and hint.auto_merge:
                counts["auto_merge"] += 1
            elif hint is not None:
                counts["gray"] += 1
            else:
                counts["strong"] += 1
        merge_pending = int(
            ctx.conn.execute(
                text(
                    "SELECT count(*) FROM mod_identidades_merge_candidates "
                    "WHERE user_id = :uid AND status = 'candidate'"
                ),
                {"uid": ctx.user_id},
            ).scalar_one()
        )
        _log.info(
            "identidades.dedup.done",
            n=len(mentions),
            **counts,
            merge_pending=merge_pending,
            duration_ms=int((time.monotonic() - t0) * 1000),
            inbox_ids=list(ctx.inbox_ids),
        )
        return len(mentions)

    def _trace_mention(
        self, ctx: ModuleContext, m: IdentityItem, res: Resolution, hint: _MergeHint | None
    ) -> None:
        """Traza por mención: entidad anclada a la identidad resuelta + cómo resolvió (señal fuerte,
        auto-merge difuso, candidato en zona gris, o creada). No-op si la traza está apagada."""
        if res.identity_id is None:
            return
        ent = ctx.trace.entity(
            "mod_identidades",
            id=res.identity_id,
            label=f"«{m.name}» → {res.method}",
            status="ok",
            source_inbox_ids=m.source_inbox_ids,
        )
        if res.method == "created":
            ent.log("no se parece a nada → creada nueva", status="info")
        elif hint is not None and hint.auto_merge:
            ent.decision(
                f"auto-merge con #{hint.other_id}",
                ref=("mod_identidades", hint.other_id),
                detail={"trgm": round(hint.score, 3), "umbral": HIGH_THRESHOLD},
                status="ok",
            )
        elif hint is not None:  # zona gris → candidato para el desempate LLM (FASE 2)
            ent.step("dedup · zona gris").decision(
                f"vs #{hint.other_id}",
                ref=("mod_identidades", hint.other_id),
                detail={
                    "trgm": round(hint.score, 3),
                    "umbral_low": LOW_THRESHOLD,
                    "umbral_high": HIGH_THRESHOLD,
                    "estado": "candidato → desempate LLM",
                },
                status="warn",
            )
        else:
            ent.log(f"resuelta por {res.method}", status="ok")

    async def health_check(self) -> HealthResult:
        return HealthResult(
            status="healthy", detail="identidades module ready", checked_at=datetime.now(UTC)
        )

    def provide_domain(self, conn: Connection, user_id: int) -> IdentidadesDomainReader:
        """Capacidad `provide_domain`: handle de LECTURA del directorio. Lo reciben (vía
        `ctx.deps['identidades']`) los módulos que declaran `'identidades'` en `depends_on`:
        resuelve referencias (nombre/email/handle) a la identidad canónica con la lógica del
        dedup."""
        return IdentidadesDomainReader(conn, user_id)

    def read_for_inbox(
        self, conn: Connection, user_id: int, inbox_ids: Sequence[int]
    ) -> list[dict[str, Any]]:
        """Menciones públicas atribuidas a `inbox_ids` (quién se nombró + cómo resolvió). NO expone
        el id interno de resolución (`resolved_identity_id`) ni el directorio."""
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
        huérfana. NO toca el directorio (identidades), que trasciende al mensaje."""
        return forget_inbox_rows(
            conn, "mod_identidades_mentions", user_id=user_id, inbox_ids=inbox_ids
        )


# --- helpers -------------------------------------------------------------------------- #


def _resolve_fuzzy_or_create(
    conn: Connection,
    user_id: int,
    m: IdentityItem,
    index: KnownIndex,
    *,
    source: str = "extraction",
) -> tuple[Resolution, _MergeHint | None]:
    """Sin match exacto: difuso. ≥HIGH auto-merge (+alias); zona gris crea provisional + encola
    candidato; <LOW crea nueva. `source` se propaga a la entidad creada ('extraction' por default;
    'manual' desde una tarjeta). Devuelve el vecino difuso para la traza (None si nueva)."""
    kind = _identity_kind(m.kind)
    candidates = find_fuzzy_candidates(conn, user_id, kind=kind, probe=m.name)
    best = candidates[0] if candidates else None
    if best is not None and best.score >= HIGH_THRESHOLD:
        _add_alias(conn, user_id, best.identity_id, m.name)
        index.add_alias(m.name, best.identity_id)
        return Resolution(kind, best.identity_id, "fuzzy"), _MergeHint(
            best.identity_id, best.score, auto_merge=True
        )
    new_id = _create_entity(conn, user_id, m, kind, index, source=source)
    hint: _MergeHint | None = None
    if best is not None and best.score >= LOW_THRESHOLD:
        # zona gris (trigram): candidato para el desempate LLM (par canónico a<b).
        _propose_merge_candidate(conn, user_id, new_id, best.identity_id, "trgm_name", best.score)
        hint = _MergeHint(best.identity_id, best.score, auto_merge=False)
    # H-7: 2ª fuente de candidatos por CONTENCIÓN DE TOKENS (subcadena/abreviación del mismo nombre)
    # que el trigram no alcanza. Solo CANDIDATOS para el juez LLM — NUNCA auto-merge. Se propone
    # después del trigram para que, en un par solapado, gane su reason (ON CONFLICT).
    for c in find_containment_candidates(conn, user_id, kind=kind, probe=m.name, exclude_id=new_id):
        _propose_merge_candidate(conn, user_id, new_id, c.identity_id, "token_containment", c.score)
    return Resolution(kind, new_id, "fuzzy" if hint is not None else "created"), hint


def _create_entity(
    conn: Connection,
    user_id: int,
    item: IdentityItem,
    kind: str,
    index: KnownIndex,
    *,
    source: str = "extraction",
) -> int:
    """Crea una identidad NUEVA (interest=FALSE), vuelca email/handle a identificadores, y la
    registra en el índice para el dedup intra-corrida. `source` distingue la procedencia
    ('extraction' por default; 'manual' cuando la siembra una tarjeta). Devuelve el id."""
    new_id = int(
        conn.execute(
            text(
                """
                INSERT INTO mod_identidades (user_id, kind, display_name, source, interest)
                VALUES (:u, :k, :n, :src, FALSE)
                RETURNING id
                """
            ),
            {"u": user_id, "k": kind, "n": item.name, "src": source},
        ).scalar_one()
    )
    identifiers: list[KnownIdentifier] = []
    # Una dirección role/relay (noreply, notifications, …) NO es clave de identidad → no se guarda
    # como identificador (si no, fusionaría remitentes distintos que comparten el relay).
    if item.email and not is_role_email(item.email):
        vn = norm_identifier("email", item.email)
        _insert_identifier(conn, user_id, new_id, "email", "email", item.email, vn, source=source)
        identifiers.append(KnownIdentifier("email", "email", vn))
    if item.handle:
        vn = norm_identifier("handle", item.handle)
        _insert_identifier(
            conn, user_id, new_id, "unknown", "handle", item.handle, vn, source=source
        )
        identifiers.append(KnownIdentifier("unknown", "handle", vn))
    index.add(
        KnownIdentity(id=new_id, kind=kind, display_name=item.name, identifiers=tuple(identifiers))
    )
    return new_id


def _insert_identifier(
    conn: Connection,
    user_id: int,
    identity_id: int,
    platform: str,
    kind: str,
    value: str,
    vn: str,
    *,
    source: str = "extraction",
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mod_identidades_identifiers
              (user_id, identity_id, platform, kind, value, value_norm, source)
            VALUES (:u, :iid, :p, :k, :v, :vn, :src)
            ON CONFLICT (identity_id, platform, kind, value_norm) DO NOTHING
            """
        ),
        {
            "u": user_id,
            "iid": identity_id,
            "p": platform,
            "k": kind,
            "v": value,
            "vn": vn,
            "src": source,
        },
    )


def _add_alias(conn: Connection, user_id: int, identity_id: int, name: str) -> None:
    """Suma `name` a los alias de la identidad (sin duplicar el display_name ni un alias ya
    presente). Lo usa el auto-merge para que el próximo match sea exacto."""
    conn.execute(
        text(
            """
            UPDATE mod_identidades
            SET aliases = (
                  SELECT array_agg(DISTINCT x) FROM unnest(aliases || ARRAY[:name]) AS x
                ),
                updated_at = NOW()
            WHERE id = :id AND user_id = :u AND display_name <> :name AND NOT (:name = ANY(aliases))
            """
        ),
        {"id": identity_id, "u": user_id, "name": name},
    )


def _propose_merge_candidate(
    conn: Connection, user_id: int, a_id: int, b_id: int, reason: str, score: float
) -> None:
    """Encola un par (canónico a<b) como candidato de merge para el desempate LLM (idempotente)."""
    lo, hi = (a_id, b_id) if a_id < b_id else (b_id, a_id)
    conn.execute(
        text(
            """
            INSERT INTO mod_identidades_merge_candidates
              (user_id, identity_a_id, identity_b_id, reason, score)
            VALUES (:u, :a, :b, :reason, :score)
            ON CONFLICT (identity_a_id, identity_b_id) DO NOTHING
            """
        ),
        {"u": user_id, "a": lo, "b": hi, "reason": reason, "score": score},
    )


def _insert_mention(
    conn: Connection,
    user_id: int,
    item: IdentityItem,
    res: Resolution,
    *,
    event_id: str | None = None,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mod_identidades_mentions
              (user_id, source_inbox_ids, evidence, mentioned_name, mentioned_kind, email, handle,
               org_hint, role_hint, confidence, resolved_kind, resolved_identity_id,
               resolution_method, event_id)
            VALUES (:uid, :ids, :evidence, :name, :kind, :email, :handle, :org, :role, :confidence,
                    :rkind, :rid, :method, :event_id)
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
            "rid": res.identity_id,
            "method": res.method,
            "event_id": event_id,
        },
    )


def load_known_index(conn: Connection, user_id: int) -> KnownIndex:
    """Arma el `KnownIndex` del user desde `mod_identidades` + `mod_identidades_identifiers`.
    Compartido por `dedup` (extracción) y el handle `provide_domain` (`domain.py`)."""
    base = {
        int(r["id"]): (str(r["kind"]), str(r["display_name"]), tuple(r["aliases"] or ()))
        # `ORDER BY id` hace DETERMINISTA el «primer match gana» del KnownIndex (gana el id MENOR):
        # el directorio se itera por id, así un nombre/identificador compartido por varias
        # identidades resuelve siempre a la misma (la más vieja), no al orden físico de las filas.
        for r in conn.execute(
            text(
                "SELECT id, kind, display_name, aliases FROM mod_identidades "
                "WHERE user_id = :uid ORDER BY id"
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    }
    by_identity: dict[int, list[KnownIdentifier]] = {}
    for r in (
        conn.execute(
            text(
                "SELECT identity_id, platform, kind, value_norm "
                "FROM mod_identidades_identifiers WHERE user_id = :uid "
                "ORDER BY identity_id, id"
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    ):
        by_identity.setdefault(int(r["identity_id"]), []).append(
            KnownIdentifier(str(r["platform"]), str(r["kind"]), str(r["value_norm"]))
        )
    return KnownIndex(
        [
            KnownIdentity(
                id=iid,
                kind=kind,
                display_name=display_name,
                aliases=aliases,
                identifiers=tuple(by_identity.get(iid, ())),
            )
            for iid, (kind, display_name, aliases) in base.items()
        ]
    )


# --- alta por tarjeta (resolve-or-create manual, sin mención) ------------------------- #


def resolve_or_create_identity(
    conn: Connection, user_id: int, item: IdentityItem, *, source: str = "manual"
) -> Resolution:
    """Resuelve `item` contra el directorio (señales fuertes + difuso) y crea si no existe — la
    MISMA lógica que `dedup` usa en la extracción, pero SIN registrar mención (una tarjeta no es
    evidencia de un mensaje). Idempotente. Devuelve la `Resolution` (merge/candidato/creada)."""
    index = load_known_index(conn, user_id)
    res = index.resolve(item)
    if res.kind is None:
        res, _hint = _resolve_fuzzy_or_create(conn, user_id, item, index, source=source)
    return res


def _store_card_identifiers(
    conn: Connection,
    user_id: int,
    identity_id: int,
    *,
    email: str | None,
    handle: str | None,
    phone: str | None,
    source: str = "manual",
) -> None:
    """Vuelca los identificadores de la tarjeta a la identidad resuelta (idempotente por el UNIQUE):
    enriquece el directorio aun si resolvió a una identidad existente. El email role/relay (noreply,
    …) NO es clave de identidad → no se guarda (igual que la extracción)."""
    if email and not is_role_email(email):
        vn = norm_identifier("email", email)
        if vn:
            _insert_identifier(
                conn, user_id, identity_id, "email", "email", email, vn, source=source
            )
    if handle:
        vn = norm_identifier("handle", handle)
        if vn:
            _insert_identifier(
                conn, user_id, identity_id, "unknown", "handle", handle, vn, source=source
            )
    if phone:
        vn = norm_identifier("phone", phone)
        if vn:
            _insert_identifier(
                conn, user_id, identity_id, "phone", "phone", phone, vn, source=source
            )


def _public_identity(conn: Connection, user_id: int, identity_id: int) -> dict[str, Any]:
    """La fila pública de una identidad (lo que la CLI del agente devuelve en `--json`)."""
    r = (
        conn.execute(
            text(
                """
                SELECT id, kind, display_name, source, interest, created_at, updated_at
                FROM mod_identidades WHERE id = :id AND user_id = :u
                """
            ),
            {"id": identity_id, "u": user_id},
        )
        .mappings()
        .one()
    )
    return dict(r)


def _affiliate(
    conn: Connection,
    user_id: int,
    person_id: int,
    org_id: int,
    role: str | None,
    *,
    source: str = "manual",
) -> None:
    """Enlaza persona↔org en el directorio (idempotente; re-correr con otro rol lo actualiza)."""
    conn.execute(
        text(
            """
            INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id, role, source)
            VALUES (:u, :p, :o, :role, :src)
            ON CONFLICT (person_id, org_id) DO UPDATE SET role = EXCLUDED.role
            """
        ),
        {"u": user_id, "p": person_id, "o": org_id, "role": role, "src": source},
    )


def register_card(
    conn: Connection,
    user_id: int,
    *,
    name: str,
    kind: str,
    email: str | None = None,
    handle: str | None = None,
    phone: str | None = None,
    org: str | None = None,
    role: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Resolve-or-create de UNA identidad desde una tarjeta de contacto (manual, sin LLM): la pasa
    por la MISMA resolución que la extracción y vuelca sus identificadores (email/handle/phone). Si
    trae `org` (solo personas), asegura la organización, teje la afiliación persona↔org y su arista
    `afiliado` en el grafo. Con `event_id` (cierre de un evento del agente) registra además la
    MENCIÓN-evento (la evidencia del avistamiento) y teje incremental las aristas `mismo_evento`
    con los otros hechos del evento; la org de `--org` NO recibe mención-evento (ya queda atada por
    `afiliado`). Idempotente. Devuelve la fila pública resuelta (+ `method` y, si hubo, `org`).
    Escribe todo en `conn` (atómico con la tx del caller)."""
    if kind not in (KIND_PERSONA, KIND_ORG, KIND_PRODUCTO):
        raise ValueError(
            f"kind inválido: {kind!r} (esperado 'persona', 'organizacion' o 'producto')"
        )
    if org and kind != KIND_PERSONA:
        raise ValueError("'org' solo aplica a una persona (la afiliación es persona↔organización)")

    item = IdentityItem.model_validate(
        {
            "source_inbox_ids": (),
            "name": name,
            "kind": kind,
            "email": email,
            "handle": handle,
            "org": org,
            "role": role,
            "evidence": f"agent:{event_id}" if event_id else "",
        }
    )
    res = resolve_or_create_identity(conn, user_id, item, source="manual")
    assert res.identity_id is not None  # resolve_or_create_identity siempre ata o crea
    if event_id:
        _insert_mention(conn, user_id, item, res, event_id=event_id)
        weave_event(conn, user_id, event_id)
    _store_card_identifiers(conn, user_id, res.identity_id, email=email, handle=handle, phone=phone)
    result = _public_identity(conn, user_id, res.identity_id)
    result["method"] = res.method

    if org and res.kind == KIND_PERSONA:
        org_item = IdentityItem.model_validate(
            {"source_inbox_ids": (), "name": org, "kind": "organizacion"}
        )
        ores = resolve_or_create_identity(conn, user_id, org_item, source="manual")
        assert ores.identity_id is not None
        _affiliate(conn, user_id, res.identity_id, ores.identity_id, role)
        weave_afiliacion(conn, user_id, res.identity_id)
        org_row = _public_identity(conn, user_id, ores.identity_id)
        org_row["method"] = ores.method
        org_row["role"] = role
        result["org"] = org_row

    return result


__all__ = [
    "IdentidadesModule",
    "load_known_index",
    "register_card",
    "resolve_or_create_identity",
]
