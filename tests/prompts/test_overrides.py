"""Overrides: guardar aplica en el proceso (todos los targets), deja versión, avisa si faltan los
principios; restablecer vuelve al código; volver a una versión; `refresh_if_stale` reaplica lo
que otro proceso guardó y restaura lo que otro proceso restableció."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from memex.db import connection
from memex.prompts import overrides
from memex.prompts.registry import default_text, read_target, spec, write_target

KEY = "tareas.judge_done"


@pytest.fixture(autouse=True)
def _limpio() -> Iterator[None]:
    yield
    # dejar el proceso como el código (otros tests leen estos prompts)
    for t in spec(KEY).targets:
        write_target(t, default_text(KEY))
    for t in spec("tareas.extract").targets:
        write_target(t, default_text("tareas.extract"))
    overrides._mark_dirty()


def test_guardar_aplica_versiona_y_restablecer_vuelve_al_codigo() -> None:
    base = default_text(KEY)
    nuevo = base + "\nRegla extra del dueño."
    with connection() as c:
        v = overrides.set_override(c, KEY, nuevo, note="prueba")
    assert v.override == nuevo and v.effective == nuevo and v.note == "prueba"
    assert all(read_target(t) == nuevo for t in spec(KEY).targets)
    assert [x.note for x in v.versions] == ["prueba"]
    with connection() as c:
        listed = {x.key: x for x in overrides.list_views(c)}
    assert listed[KEY].override == nuevo and listed["tareas.extract"].override is None
    with connection() as c:
        v2 = overrides.reset_override(c, KEY)
    assert v2.override is None and v2.effective == base
    assert all(read_target(t) == base for t in spec(KEY).targets)
    assert next(x.note for x in v2.versions) == "restablecido al código"
    # volver a la versión editada
    vid = next(x.id for x in v2.versions if x.note == "prueba")
    with connection() as c:
        v3 = overrides.revert_to_version(c, KEY, vid)
    assert v3.override == nuevo and read_target(spec(KEY).targets[0]) == nuevo


def test_validacion() -> None:
    with pytest.raises(ValueError):
        overrides.validate(KEY, "   ")
    with pytest.raises(ValueError):
        overrides.validate("principios", "x")  # referencia, no editable
    clean, warns = overrides.validate("tareas.extract", "Sos un extractor. Respondé JSON.")
    assert clean and any("principios" in w for w in warns)
    # guardar el texto del código = restablecer (no queda override)
    with connection() as c:
        v = overrides.set_override(c, KEY, default_text(KEY))
    assert v.override is None


def test_refresh_reaplica_lo_de_otro_proceso() -> None:
    base = default_text(KEY)
    nuevo = base + "\nOtro proceso lo cambió."
    with connection() as c:
        overrides.set_override(c, KEY, nuevo)
    # simular OTRO proceso: en este, el atributo quedó viejo
    for t in spec(KEY).targets:
        write_target(t, base)
    assert overrides.refresh_if_stale(force=True)
    assert read_target(spec(KEY).targets[0]) == nuevo
    # y si otro proceso restableció, el refresco vuelve al código
    with connection() as c:
        c.execute(
            __import__("sqlalchemy").text("DELETE FROM prompt_overrides WHERE key = :k"), {"k": KEY}
        )
    assert overrides.refresh_if_stale(force=True)
    assert read_target(spec(KEY).targets[0]) == base
