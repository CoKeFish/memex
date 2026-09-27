"""Nivel de compromiso: normalización, juez con LLM falso, regla de corroboración (pago a la
organización), máximo por grupo en la consolidación, conflictos solo confirmado/provisional, CLI."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import date, time
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.calendar.cli import main as cal_main
from memex.modules.calendar.commitment import (
    ANUNCIO,
    CONFIRMADO,
    PROVISIONAL,
    apply_upgrade_rules,
    max_level,
    normalize_level,
    parse_verdict,
    run_classify,
)
from memex.modules.calendar.consolidate import run_consolidation
from memex.modules.calendar.schema import CalendarEventItem


def _last_json(capsys: pytest.CaptureFixture[str]) -> Any:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _inbox(subject: str, body: str, sender: str = "info@britano.edu.co") -> int:
    with connection() as c:
        src = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type, enabled, config) "
                "VALUES (1, :n, 'email', TRUE, '{}'::jsonb) RETURNING id"
            ),
            {"n": f"test-{uuid.uuid4().hex[:8]}"},
        ).scalar_one()
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, received_at, "
                    "payload) VALUES (1, :s, :x, NOW(), NOW(), CAST(:p AS JSONB)) RETURNING id"
                ),
                {
                    "s": src,
                    "x": f"x-{subject}",
                    "p": json.dumps({"subject": subject, "from": sender, "body_text": body}),
                },
            ).scalar_one()
        )


def _extracted(
    title: str,
    *,
    inbox_id: int,
    starts_on: date = date(2026, 9, 16),
    start_time: time | None = time(15, 10),
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_events (user_id, source_inbox_ids, title, starts_on, "
                    "start_time, origin) VALUES (1, CAST(:ids AS BIGINT[]), :t, :d, :st, "
                    "'extraction') "
                    "RETURNING id"
                ),
                {"ids": [inbox_id], "t": title, "d": starts_on, "st": start_time},
            ).scalar_one()
        )


def test_normalize_and_max_level() -> None:
    assert (
        normalize_level("CONFIRMADO") == CONFIRMADO and normalize_level("confirmed") == CONFIRMADO
    )
    assert normalize_level("tentativo") == PROVISIONAL and normalize_level("anuncio") == ANUNCIO
    # Los nombres viejos del campo se siguen entendiendo (un LLM puede responder con ellos).
    assert normalize_level("firme") == CONFIRMADO and normalize_level("probable") == PROVISIONAL
    assert normalize_level("informativo") == ANUNCIO
    assert normalize_level(None) == ANUNCIO and normalize_level("raro", default="") == ""
    assert max_level([ANUNCIO, PROVISIONAL]) == PROVISIONAL
    assert max_level([PROVISIONAL, CONFIRMADO, ANUNCIO]) == CONFIRMADO
    assert max_level([]) == ANUNCIO


def test_parse_verdict_tolerant() -> None:
    v = parse_verdict('{"level": "Firme", "confidence": "0.9", "evidence": "tu cupo"}')
    assert (v.level, v.confidence, v.evidence) == (CONFIRMADO, 0.9, "tu cupo")
    assert parse_verdict("no json").level == ANUNCIO
    assert parse_verdict('{"level": "probable", "confidence": 7}').confidence == 1.0


def test_extraction_item_carries_commitment() -> None:
    item = CalendarEventItem.model_validate(
        {
            "source_inbox_ids": [1],
            "title": "Clase",
            "starts_on": "2026-09-02",
            "evidence": "e",
            "commitment": "inscrita",
            "commitment_evidence": "te confirmamos tu cupo",
        }
    )
    assert item.commitment == CONFIRMADO and item.commitment_evidence == "te confirmamos tu cupo"
    plain = CalendarEventItem.model_validate(
        {"source_inbox_ids": [1], "title": "x", "starts_on": "2026-09-02", "evidence": "e"}
    )
    assert plain.commitment == ANUNCIO


class _FakeLLM:
    """Devuelve un veredicto según el asunto del mensaje que aparece en el prompt."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.calls += 1
        prompt = messages[-1].content
        if "tu matrícula" in prompt:
            body = {
                "level": "confirmado",
                "confidence": 0.98,
                "evidence": "te confirmamos tu matrícula",
            }
        elif "te esperamos" in prompt:
            body = {"level": "provisional", "confidence": 0.8, "evidence": "te esperamos"}
        else:
            body = {
                "level": "anuncio",
                "confidence": 0.97,
                "evidence": "inscripciones abiertas",
            }
        return LLMResult(
            content=json.dumps(body),
            model="fake",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=0,
        )


@pytest.mark.asyncio
async def test_classify_judges_pending_extracted_events_and_consolidation_takes_max() -> None:
    firm = _extracted(
        "Clase de inglés",
        inbox_id=_inbox("Confirmación", "Hola, te confirmamos tu matrícula al intensivo."),
    )
    prob = _extracted(
        "Taller de robótica",
        inbox_id=_inbox("Taller", "El jueves te esperamos en el taller."),
        starts_on=date(2026, 9, 17),
    )
    info = _extracted(
        "Feria",
        inbox_id=_inbox("Feria", "Inscripciones abiertas para la feria de todos."),
        starts_on=date(2026, 9, 18),
    )
    llm = _FakeLLM()
    stats = await run_classify(1, since_days=3650, client=llm)
    assert (stats.judged, stats.confirmado, stats.provisional, stats.anuncio) == (3, 1, 1, 1)
    assert llm.calls == 3
    # idempotente: ya tienen fuente → no se vuelven a juzgar
    assert (await run_classify(1, since_days=3650, client=llm)).judged == 0 and llm.calls == 3
    with connection() as c:
        levels = {
            int(i): (str(cm), str(src))
            for i, cm, src in c.execute(
                text("SELECT id, commitment, commitment_source FROM mod_calendar_events")
            ).all()
        }
    assert levels[firm] == (CONFIRMADO, "judge") and levels[prob] == (PROVISIONAL, "judge")
    assert levels[info] == (ANUNCIO, "judge")
    run_consolidation(1)
    with connection() as c:
        cons = {
            str(t): (str(cm), str(ev))
            for t, cm, ev in c.execute(
                text("SELECT title, commitment, commitment_evidence FROM mod_calendar_consolidated")
            ).all()
        }
    assert cons["Clase de inglés"] == (CONFIRMADO, "te confirmamos tu matrícula")
    assert cons["Feria"][0] == ANUNCIO


def test_upgrade_rule_only_lifts_probable_when_the_person_paid_the_sender_org() -> None:
    with connection() as c:
        org = c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1, 'organizacion', 'Centro Britano Americano') RETURNING id"
            )
        ).scalar_one()
    paid_inbox = _inbox("Tu clase", "te esperamos el jueves")
    other_inbox = _inbox("Curso abierto", "inscripciones abiertas", sender="cursos@otro.org")
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_identidades_mentions (user_id, source_inbox_ids, evidence, "
                "mentioned_name, mentioned_kind, resolved_kind, resolved_identity_id, "
                "resolution_method) VALUES (1, CAST(:ids AS BIGINT[]), 'from', 'Britano', "
                "'organizacion', 'organizacion', :o, 'exact_name')"
            ),
            {"ids": [paid_inbox], "o": org},
        )
        c.execute(
            text(
                "INSERT INTO mod_finance_consolidated (user_id, direction, status, kind, amount, "
                "currency, category, counterparty, counterparty_identity_id, occurred_at) VALUES "
                "(1, 'egreso', 'completada', 'pago', 874000, 'COP', 'educacion', 'Britano', "
                ":o, NOW())"
            ),
            {"o": org},
        )
    probable_paid = _extracted("Clase", inbox_id=paid_inbox)
    probable_unpaid = _extracted("Taller", inbox_id=other_inbox, starts_on=date(2026, 9, 17))
    informativo_paid = _extracted(
        "Feria del Britano", inbox_id=paid_inbox, starts_on=date(2026, 9, 18)
    )
    with connection() as c:
        for eid, lv in (
            (probable_paid, PROVISIONAL),
            (probable_unpaid, PROVISIONAL),
            (informativo_paid, ANUNCIO),
        ):
            c.execute(
                text(
                    "UPDATE mod_calendar_events SET commitment = :c, "
                    "commitment_source = 'judge' WHERE id = :i"
                ),
                {"c": lv, "i": eid},
            )
        assert apply_upgrade_rules(c, 1) == 1
        levels = {
            int(i): str(cm)
            for i, cm in c.execute(text("SELECT id, commitment FROM mod_calendar_events")).all()
        }
    assert levels[probable_paid] == CONFIRMADO  # provisional + pago a la org → confirmado
    assert levels[probable_unpaid] == PROVISIONAL
    assert levels[informativo_paid] == ANUNCIO  # el pago NO sube un anuncio


def test_commitment_cli_sets_level_and_drives_conflicts(capsys: pytest.CaptureFixture[str]) -> None:
    """Un anuncio no entra en conflicto con nada; al marcarlo confirmado (tu palabra) entra en
    conflicto con la cita fijada de la misma hora, y list/show muestran el estado."""
    _extracted("Clase de inglés", inbox_id=_inbox("x", "inscripciones abiertas"))
    run_consolidation(1)
    with connection() as c:
        cons_id = int(c.execute(text("SELECT id FROM mod_calendar_consolidated")).scalar_one())
    cal_main(
        ["add", "--title", "Odontología", "--date", "2026-09-16", "--time", "15:30", "--fijado"]
    )
    with connection() as c:
        assert c.execute(text("SELECT count(*) FROM mod_calendar_conflicts")).scalar() == 0
    assert cal_main(["estado", str(cons_id), "confirmado", "--json"]) == 0
    assert _last_json(capsys)["commitment"] == CONFIRMADO
    with connection() as c:
        assert c.execute(text("SELECT count(*) FROM mod_calendar_conflicts")).scalar() == 1
        assert (
            c.execute(
                text(
                    "SELECT commitment_source FROM mod_calendar_events "
                    "WHERE title = 'Clase de inglés'"
                )
            ).scalar_one()
            == "user"
        )
    cal_main(["list", "--since", "2026-09-16", "--until", "2026-09-16", "--json"])
    items = {i["title"]: i["commitment"] for i in _last_json(capsys)["items"]}
    assert items == {
        "Clase de inglés": CONFIRMADO,
        "Odontología": CONFIRMADO,
    }  # manual = confirmado
    cal_main(["show", str(cons_id)])
    assert "estado:      confirmado" in capsys.readouterr().out
    with pytest.raises(SystemExit):  # argparse: nivel fuera de las opciones
        cal_main(["estado", str(cons_id), "firme"])  # el nombre viejo ya no es una opción
