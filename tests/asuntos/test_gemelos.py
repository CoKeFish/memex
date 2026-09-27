"""Gemelos de asuntos: el asignador NO abre un asunto que ya es una cosa del catálogo. Nombre o
alias igual redirige sin juez; entidades en común o parecido por significado proponen y el juez
(`asuntos_judge`) decide; sin juez solo valen los exactos. Lo que el lote le atribuía al «nuevo»
(evidencia, hito, estado) va al existente y el nombre propuesto queda como alias."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import LLMResult, LLMUsage
from memex.llm.config import LLMConfigError
from memex.modules.asuntos import gemelos, store
from memex.modules.asuntos.assign import SYSTEM_PROMPT, run_assign
from memex.modules.asuntos.settings import AsuntosSettings
from memex.semantic.embedder import FixedEmbedder, use_embedder
from tests.asuntos._seed import FakeLLM, asunto, msg, source

_T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

BECA = "Beca Viaje 2026 (Fundación)"
BECA_DESC = (
    "Beca de la fundación (viaje y alojamiento) para ir a HackLisboa 2026, 25-26 de octubre."
)
HACK = "HackLisboa 2026"
HACK_DESC = "Aplicación enviada para participar en HackLisboa 2026, 25 y 26 de octubre."
OTRO = "Curso de cerámica"
OTRO_DESC = "Curso de cerámica los sábados."


class _Juez:
    """Juez de mentira: `same` fijo, cuenta llamadas y guarda el último prompt."""

    def __init__(self, same: bool, confidence: float = 0.9) -> None:
        self.same = same
        self.confidence = confidence
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, messages: Any, **_: Any) -> LLMResult:
        self.calls += 1
        self.prompts.append(messages[-1].content)
        return LLMResult(
            content=json.dumps({"same": self.same, "confidence": self.confidence, "reason": "x"}),
            model="fake-judge",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost_usd=Decimal("0"),
            latency_ms=1,
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _sin_juez_real(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def _no(_uid: int) -> Any:
        raise LLMConfigError("sin proveedor en tests")

    monkeypatch.setattr(gemelos, "_judge_client", _no)
    yield


def _vectors() -> FixedEmbedder:
    return FixedEmbedder(
        {
            gemelos.asunto_text(BECA, BECA_DESC): [1.0, 0.95, 0.0],
            gemelos.asunto_text(HACK, HACK_DESC): [0.95, 1.0, 0.0],
            gemelos.asunto_text(OTRO, OTRO_DESC): [0.0, 0.0, 1.0],
        },
        dim=3,
    )


def _resp(m: int, *, name: str = HACK, description: str = HACK_DESC, ents: list[str]) -> str:
    return json.dumps(
        {
            "asignaciones": [
                {
                    "inbox_id": m,
                    "asunto": "nuevo:1",
                    "confianza": 0.95,
                    "hito": {"fecha": "2026-09-20", "que": "Aplicación enviada"},
                }
            ],
            "nuevos": [
                {
                    "ref": "nuevo:1",
                    "nombre": name,
                    "tipo": "hackathon",
                    "descripcion": description,
                    "entidades": ents,
                    "semilla": "registro",
                }
            ],
            "actualizaciones": [
                {"asunto": "nuevo:1", "estado": "aplicación enviada", "cerrado": False}
            ],
            "relaciones": [],
        }
    )


def _set(aid: int, *, description: str, entities: list[str], aliases: tuple[str, ...] = ()) -> None:
    with connection() as c:
        c.execute(
            text(
                "UPDATE mod_asuntos SET description = :d, entities = :e, aliases = :al "
                "WHERE id = :a"
            ),
            {"d": description, "e": entities, "al": list(aliases), "a": aid},
        )


def _names() -> list[str]:
    with connection() as c:
        return [a.name for a in store.list_asuntos(c, 1)]


def test_prompt_dice_una_cosa_del_mundo_un_asunto() -> None:
    assert "UNA COSA DEL MUNDO, UN ASUNTO" in SYSTEM_PROMPT
    assert "NUNCA abre uno nuevo" in SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_nombre_o_alias_igual_redirige_sin_juez() -> None:
    src = source()
    beca = asunto(BECA, kind="evento")
    _set(beca, description=BECA_DESC, entities=["Fundación"], aliases=(HACK,))
    m = msg(src, subject="You're on the list", from_addr="team@hack.org", occurred=_T0)
    llm = FakeLLM(_resp(m, ents=["Hack Lisboa"]))
    res = await run_assign(1, [m], client=llm, settings=AsuntosSettings())
    assert res.created == 0 and res.twins == 1 and res.assigned == 1 and res.hitos == 1
    assert HACK not in _names()
    with connection() as c:
        a = store.get_asunto(c, 1, beca)
        ev = store.evidence_of(c, 1, beca)
    assert a is not None and a.state_text == "aplicación enviada"
    assert "Hack Lisboa" in a.entities
    assert [e.inbox_id for e in ev] == [m] and ev[0].hito_text == "Aplicación enviada"


@pytest.mark.asyncio
async def test_parecido_por_significado_va_al_juez_y_si_afirma_redirige(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = source()
    beca = asunto(BECA, kind="evento")
    _set(beca, description=BECA_DESC, entities=["Fundación", "Lisboa"])
    otro = asunto(OTRO, kind="programa")
    _set(otro, description=OTRO_DESC, entities=[])
    juez = _Juez(same=True)
    monkeypatch.setattr(gemelos, "_judge_client", lambda _uid: juez)
    m = msg(src, subject="You're on the list", from_addr="team@hack.org", occurred=_T0)
    with use_embedder(_vectors()):
        res = await run_assign(
            1, [m], client=FakeLLM(_resp(m, ents=["Hack"])), settings=AsuntosSettings()
        )
    assert res.created == 0 and res.twins == 1
    assert juez.calls == 1 and BECA in juez.prompts[0] and HACK in juez.prompts[0]
    with connection() as c:
        a = store.get_asunto(c, 1, beca)
        judged = c.execute(
            text("SELECT count(*) FROM llm_calls WHERE user_id = 1 AND purpose = 'asuntos_judge'")
        ).scalar_one()
    assert a is not None and HACK in a.aliases and "Hack" in a.entities
    assert a.name == BECA  # el nombre vigente no cambia
    assert judged >= 1


@pytest.mark.asyncio
async def test_si_el_juez_dice_distinta_nace_el_nuevo(monkeypatch: pytest.MonkeyPatch) -> None:
    src = source()
    beca = asunto(BECA, kind="evento")
    _set(beca, description=BECA_DESC, entities=["Fundación"])
    juez = _Juez(same=False)
    monkeypatch.setattr(gemelos, "_judge_client", lambda _uid: juez)
    m = msg(src, subject="You're on the list", from_addr="team@hack.org", occurred=_T0)
    with use_embedder(_vectors()):
        res = await run_assign(
            1, [m], client=FakeLLM(_resp(m, ents=["Hack"])), settings=AsuntosSettings()
        )
    assert res.created == 1 and res.twins == 0 and juez.calls == 1
    assert HACK in _names()


@pytest.mark.asyncio
async def test_sin_juez_solo_valen_los_exactos() -> None:
    src = source()
    beca = asunto(BECA, kind="evento")
    _set(beca, description=BECA_DESC, entities=["Fundación", "Hack Lisboa"])
    m = msg(src, subject="You're on the list", from_addr="team@hack.org", occurred=_T0)
    with use_embedder(_vectors()):
        res = await run_assign(
            1,
            [m],
            client=FakeLLM(_resp(m, ents=["Fundación", "Hack Lisboa"])),
            settings=AsuntosSettings(),
        )
    assert res.created == 1 and res.twins == 0  # vecina semántica + 2 entidades, pero sin juez
    assert HACK in _names()


@pytest.mark.asyncio
async def test_entidades_en_comun_proponen_sin_vectores(monkeypatch: pytest.MonkeyPatch) -> None:
    src = source()
    beca = asunto(BECA, kind="evento")
    _set(beca, description=BECA_DESC, entities=["Fundación", "Hack Lisboa", "Guayo"])
    juez = _Juez(same=True)
    monkeypatch.setattr(gemelos, "_judge_client", lambda _uid: juez)
    m = msg(src, subject="You're on the list", from_addr="team@hack.org", occurred=_T0)
    with use_embedder(None):
        res = await run_assign(
            1,
            [m],
            client=FakeLLM(_resp(m, ents=["Hack Lisboa", "Guayo"])),
            settings=AsuntosSettings(),
        )
    assert res.created == 0 and res.twins == 1 and juez.calls == 1
    assert HACK not in _names()


def test_find_twin_candidates_reglas_baratas(conn: Any) -> None:
    beca = asunto(BECA, kind="evento")
    _set(beca, description=BECA_DESC, entities=["Fundación", "Hack Lisboa"])
    pagos = asunto("Pagos Hack Lisboa", kind="servicio")
    _set(pagos, description="", entities=["Hack Lisboa", "Fundación"])
    cand = asunto("HackLisboa 2026 satélite Bogotá", kind="hackathon", status="candidato")
    with connection() as c:
        cat = [a for a in store.list_asuntos(c, 1) if a.id in (beca, pagos, cand)]
    p = gemelos.Proposed("nuevo:1", HACK, "hackathon", HACK_DESC, ("Hack Lisboa", "Fundación"))
    with use_embedder(None):
        out = gemelos.find_twin_candidates(conn, 1, p, cat)
    ids = [c.asunto.id for c in out]
    assert beca in ids  # dos entidades en común
    assert pagos not in ids  # un servicio no se empareja con otra clase
    assert cand in ids  # candidato con un token distintivo compartido («lisboa»)
    assert all(not c.exact for c in out)
    # alias igual: exacto y único
    _set(beca, description=BECA_DESC, entities=[], aliases=(HACK,))
    with connection() as c:
        cat = [a for a in store.list_asuntos(c, 1) if a.id in (beca, pagos, cand)]
    with use_embedder(None):
        out = gemelos.find_twin_candidates(conn, 1, p, cat)
    assert [(c.asunto.id, c.exact) for c in out] == [(beca, True)]
