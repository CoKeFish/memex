"""El registro de prompts apunta a atributos reales: cada target existe, es un str, y todos los
targets de una clave leen el MISMO texto (si difieren, un consumidor no vería el override).
Los consumidores nombrados existen en `LLM_CONSUMERS`."""

from __future__ import annotations

from memex.llm.settings import LLM_CONSUMERS
from memex.prompts.registry import PROMPTS, default_text, read_target, spec


def test_todos_los_targets_existen_y_coinciden() -> None:
    assert len({p.key for p in PROMPTS}) == len(PROMPTS)
    for p in PROMPTS:
        assert p.targets, p.key
        texts = {read_target(t) for t in p.targets}
        assert len(texts) == 1, (p.key, [len(t) for t in texts])
        assert default_text(p.key) == next(iter(texts))
        assert len(default_text(p.key)) > 50, p.key
        if p.consumer:
            assert p.consumer in LLM_CONSUMERS, (p.key, p.consumer)


def test_spec_desconocida() -> None:
    import pytest

    with pytest.raises(KeyError):
        spec("no.existe")
