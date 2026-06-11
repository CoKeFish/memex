"""`IdentidadesModule.dedup` (v2) sobre el directorio unificado: lo conocido se ata por señal
fuerte; lo similar-alto se AUTO-MERGEA (+alias); la zona gris crea provisional + encola candidato;
lo nuevo entra como no-interés (source='extraction'). Dedup determinista (sin LLM)."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import LLMClient
from memex.modules.contract import ModuleContext
from memex.modules.identidades.module import IdentidadesModule
from memex.modules.identidades.schema import IdentityItem


def _seed_known() -> tuple[int, int]:
    """Ada (persona, email ada@x.com) + Unity (org de interés). Devuelve (person_id, org_id)."""
    with connection() as c:
        pid = int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name, interest, source) "
                    "VALUES (1,'persona','Ada Lovelace',TRUE,'google_contacts') RETURNING id"
                )
            ).scalar_one()
        )
        c.execute(
            text(
                "INSERT INTO mod_identidades_identifiers "
                "(user_id, identity_id, platform, kind, value, value_norm) "
                "VALUES (1,:i,'email','email','ada@x.com','ada@x.com')"
            ),
            {"i": pid},
        )
        oid = int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name, interest, source) "
                    "VALUES (1,'organizacion','Unity',TRUE,'manual') RETURNING id"
                )
            ).scalar_one()
        )
    return pid, oid


def _mentions() -> dict[str, dict[str, Any]]:
    with connection() as c:
        rows = (
            c.execute(text("SELECT * FROM mod_identidades_mentions WHERE user_id=1 ORDER BY id"))
            .mappings()
            .all()
        )
    return {str(r["mentioned_name"]): dict(r) for r in rows}


def _identities(kind: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM mod_identidades WHERE user_id=1"
    if kind:
        sql += f" AND kind='{kind}'"
    with connection() as c:
        return [dict(r) for r in c.execute(text(sql + " ORDER BY id")).mappings().all()]


async def _persist(items: list[IdentityItem]) -> int:
    mod = IdentidadesModule()
    with connection() as conn:
        ctx = ModuleContext(
            user_id=1,
            conn=conn,
            llm=cast(LLMClient, None),  # dedup determinista, no usa el LLM
            deps={},
            summary_id=None,
            inbox_ids=(5, 6),
        )
        return await mod.persist(ctx, items)


@pytest.mark.asyncio
async def test_known_resolves_unknown_creates() -> None:
    pid, oid = _seed_known()
    items = [
        IdentityItem(source_inbox_ids=(5,), name="Ada L.", email="ada@x.com", evidence="con Ada"),
        IdentityItem(source_inbox_ids=(5,), name="Unity", kind="organizacion"),
        IdentityItem(source_inbox_ids=(6,), name="Zentriva Pharma", kind="organizacion"),
        IdentityItem(source_inbox_ids=(6,), name="Juan Perez", kind="persona"),
    ]
    assert await _persist(items) == 4
    m = _mentions()
    # conocidas → atadas por señal fuerte
    assert (m["Ada L."]["resolved_kind"], m["Ada L."]["resolved_identity_id"]) == ("persona", pid)
    assert m["Ada L."]["resolution_method"] == "email"
    assert m["Unity"]["resolved_kind"] == "organizacion"
    assert m["Unity"]["resolved_identity_id"] == oid
    assert m["Unity"]["resolution_method"] == "exact_name"
    # nuevas → creadas (no similares a nada existente)
    assert m["Zentriva Pharma"]["resolution_method"] == "created"
    assert m["Juan Perez"]["resolution_method"] == "created"
    # el directorio creció en no-interés / source=extraction; las conocidas no se duplicaron
    juan = next(p for p in _identities("persona") if p["display_name"] == "Juan Perez")
    assert juan["interest"] is False and juan["source"] == "extraction"
    assert m["Juan Perez"]["resolved_identity_id"] == juan["id"]
    assert sum(1 for p in _identities("persona") if p["display_name"] == "Ada Lovelace") == 1


@pytest.mark.asyncio
async def test_fuzzy_auto_merge_adds_alias() -> None:
    with connection() as c:
        oid = int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name, interest, source) "
                    "VALUES (1,'organizacion','Globex Corporation',TRUE,'manual') RETURNING id"
                )
            ).scalar_one()
        )
    # 'Globex Corp' tiene el MISMO núcleo ('globex') → similitud alta → auto-merge a la existente
    await _persist([IdentityItem(source_inbox_ids=(5,), name="Globex Corp", kind="organizacion")])
    m = _mentions()
    assert m["Globex Corp"]["resolution_method"] == "fuzzy"
    assert m["Globex Corp"]["resolved_identity_id"] == oid
    # NO se creó una org nueva; el nombre variante quedó como alias
    assert len([o for o in _identities("organizacion")]) == 1
    aliases = _identities("organizacion")[0]["aliases"]
    assert "Globex Corp" in aliases


@pytest.mark.asyncio
async def test_fuzzy_gray_zone_creates_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    # subimos el umbral ALTO para forzar la zona gris en un candidato que el query SÍ devuelve
    monkeypatch.setattr("memex.modules.identidades.module.HIGH_THRESHOLD", 0.999)
    with connection() as c:
        existing = int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name, interest, source) "
                    "VALUES (1,'organizacion','Globex',TRUE,'manual') RETURNING id"
                )
            ).scalar_one()
        )
    await _persist([IdentityItem(source_inbox_ids=(5,), name="Globexx", kind="organizacion")])
    m = _mentions()
    assert m["Globexx"]["resolution_method"] == "fuzzy"
    # se creó la provisional + se encoló un candidato de merge contra la existente
    new_org = next(o for o in _identities("organizacion") if o["display_name"] == "Globexx")
    with connection() as c:
        cand = (
            c.execute(
                text(
                    "SELECT identity_a_id, identity_b_id, status "
                    "FROM mod_identidades_merge_candidates"
                )
            )
            .mappings()
            .first()
        )
    assert cand is not None and cand["status"] == "candidate"
    assert {cand["identity_a_id"], cand["identity_b_id"]} == {existing, new_org["id"]}


@pytest.mark.asyncio
async def test_dedup_emits_aggregated_log(
    sink_capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El dedup inline emite `identidades.dedup.done` (breakdown por camino + merge_pending +
    inbox_ids de la unidad) — antes el camino caliente no dejaba NI UNA fila en log_events."""
    monkeypatch.setattr("memex.modules.identidades.module.HIGH_THRESHOLD", 0.999)
    _seed_known()
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, interest, source) "
                "VALUES (1,'organizacion','Globex',TRUE,'manual')"
            )
        )
    items = [
        IdentityItem(source_inbox_ids=(5,), name="Ada L.", email="ada@x.com"),  # señal fuerte
        IdentityItem(source_inbox_ids=(6,), name="Zentriva Pharma", kind="organizacion"),  # nueva
        IdentityItem(source_inbox_ids=(6,), name="Globexx", kind="organizacion"),  # zona gris
    ]
    assert await _persist(items) == 3

    records: list[dict[str, Any]] = []
    while not sink_capture.empty():
        records.append(sink_capture.get_nowait())
    done = [r for r in records if r["event"] == "identidades.dedup.done"]
    assert len(done) == 1, "un (1) evento agregado por unidad"
    assert done[0]["logger"] == "memex.modules.identidades"
    fields = json.loads(done[0]["fields"])
    assert fields["n"] == 3
    assert (fields["strong"], fields["auto_merge"], fields["gray"], fields["created"]) == (
        1,
        0,
        1,
        1,
    )
    assert fields["merge_pending"] == 1  # Globexx vs Globex, encolado para el desempate LLM
    assert fields["inbox_ids"] == [5, 6]
    assert fields["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_dedup_mention_failure_logs_and_reraises(
    sink_capture: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Una mención que revienta queda en log_events con su por-qué (mención + traceback) y la
    excepción SE RE-LANZA: la tx rollbackea y la ventana cae a extract.window.failed, como hoy."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("insert reventado")

    monkeypatch.setattr("memex.modules.identidades.module._insert_mention", _boom)
    with pytest.raises(RuntimeError, match="insert reventado"):
        await _persist([IdentityItem(source_inbox_ids=(5,), name="Quien Sea", kind="persona")])

    records: list[dict[str, Any]] = []
    while not sink_capture.empty():
        records.append(sink_capture.get_nowait())
    failed = [r for r in records if r["event"] == "identidades.dedup.mention_failed"]
    assert len(failed) == 1
    assert failed[0]["level"] == "error"
    assert failed[0]["exception"] is not None
    assert "insert reventado" in failed[0]["exception"]
    fields = json.loads(failed[0]["fields"])
    assert fields["name"] == "Quien Sea"
    assert fields["exc_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_dedup_within_batch_creates_once() -> None:
    items = [
        IdentityItem(source_inbox_ids=(5,), name="Initech", kind="organizacion"),
        IdentityItem(source_inbox_ids=(6,), name="initech", kind="organizacion"),  # otra grafía
    ]
    await _persist(items)
    matches = [o for o in _identities("organizacion") if o["display_name"].lower() == "initech"]
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_role_email_not_used_as_key() -> None:
    # dos remitentes distintos que comparten un From relay (notifications@github.com) NO se
    # fusionan: la dirección role no es clave de identidad → cada uno se resuelve por nombre.
    items = [
        IdentityItem(
            source_inbox_ids=(5,),
            name="ardalis",
            email="notifications@github.com",
            kind="organizacion",
        ),
        IdentityItem(
            source_inbox_ids=(6,),
            name="dependabot",
            email="notifications@github.com",
            kind="organizacion",
        ),
    ]
    await _persist(items)
    names = {o["display_name"] for o in _identities("organizacion")}
    assert {"ardalis", "dependabot"} <= names  # dos identidades distintas, no una fusionada


@pytest.mark.asyncio
async def test_sender_does_not_swallow_other_mentions() -> None:
    # Correo de Nequi (remitente conocido) que REPORTA un pago hecho en Tigo. Tigo NO debe
    # colapsarse en Nequi: el remitente es una identidad más, no la contraparte. Regresión del bug
    # Nequi→Tigo hallado en datos reales (antes el resolver probaba el email del remitente para CADA
    # mención y fundía todo en el remitente).
    with connection() as c:
        nequi = int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name, source) "
                    "VALUES (1,'organizacion','Nequi','manual') RETURNING id"
                )
            ).scalar_one()
        )
        c.execute(
            text(
                "INSERT INTO mod_identidades_identifiers "
                "(user_id, identity_id, platform, kind, value, value_norm) "
                "VALUES (1,:i,'email','email','somos@nequi.com.co','somos@nequi.com.co')"
            ),
            {"i": nequi},
        )
    items = [
        IdentityItem(
            source_inbox_ids=(5,), name="Nequi", email="somos@nequi.com.co", kind="organizacion"
        ),
        IdentityItem(source_inbox_ids=(5,), name="Tigo", kind="organizacion"),
    ]
    await _persist(items)
    m = _mentions()
    # el remitente se resuelve a sí mismo por SU email; Tigo se crea como org propia (no Nequi)
    assert m["Nequi"]["resolved_identity_id"] == nequi
    assert m["Tigo"]["resolution_method"] == "created"
    tigo = next(o for o in _identities("organizacion") if o["display_name"] == "Tigo")
    assert m["Tigo"]["resolved_identity_id"] == tigo["id"] != nequi


@pytest.mark.asyncio
async def test_producto_mention_creates_producto_identity() -> None:
    # 'producto' ya NO se pliega a organizacion (0057): crea/resuelve kind='producto'.
    # 'unknown' (el escape del extractor) sigue plegando a persona.
    items = [
        IdentityItem(source_inbox_ids=(5,), name="Hearthstone", kind="producto"),
        IdentityItem(source_inbox_ids=(6,), name="Misterio", kind="unknown"),
    ]
    assert await _persist(items) == 2
    m = _mentions()
    assert m["Hearthstone"]["mentioned_kind"] == "producto"
    assert m["Hearthstone"]["resolved_kind"] == "producto"
    hs = next(i for i in _identities("producto") if i["display_name"] == "Hearthstone")
    assert m["Hearthstone"]["resolved_identity_id"] == hs["id"]
    assert m["Misterio"]["resolved_kind"] == "persona"


@pytest.mark.asyncio
async def test_producto_strong_signal_resolves_to_existing_org() -> None:
    # Las señales FUERTES (nombre exacto) cruzan kinds a propósito: el directorio manda. Una
    # mención producto 'Steam' con la org 'Steam' ya en el directorio ata a ESA identidad (no
    # duplica); la reclasificación org→producto es trabajo del backfill por voto, no del resolver.
    with connection() as c:
        steam = int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name, source) "
                    "VALUES (1,'organizacion','Steam','manual') RETURNING id"
                )
            ).scalar_one()
        )
    await _persist([IdentityItem(source_inbox_ids=(5,), name="Steam", kind="producto")])
    m = _mentions()
    assert m["Steam"]["mentioned_kind"] == "producto"  # el voto del backfill queda registrado
    assert m["Steam"]["resolved_identity_id"] == steam
    assert m["Steam"]["resolved_kind"] == "organizacion"
    assert m["Steam"]["resolution_method"] == "exact_name"


@pytest.mark.asyncio
async def test_persist_empty_is_noop() -> None:
    assert await _persist([]) == 0
    assert _mentions() == {}
