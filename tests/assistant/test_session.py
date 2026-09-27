from __future__ import annotations

from memex.assistant.session import SessionStore, parse_confirmation


def test_parse_confirmation_short_messages_only() -> None:
    assert parse_confirmation("sí") == "yes"
    assert parse_confirmation("Si!") == "yes"
    assert parse_confirmation("dale") == "yes"
    assert parse_confirmation("ok, confirmo") == "yes"
    assert parse_confirmation("no") == "no"
    assert parse_confirmation("Cancelar.") == "no"
    assert parse_confirmation("sí, pero cambiá el monto") is None
    assert parse_confirmation("¿cuánto gasté?") is None
    assert parse_confirmation("") is None


def test_session_history_is_capped_and_isolated_per_chat() -> None:
    store = SessionStore(max_turns=2)
    a = store.get(1)
    for i in range(6):
        a.add("usuario", f"m{i}")
    assert [t for _, t in a.snapshot()] == ["m2", "m3", "m4", "m5"]
    assert store.get(1) is a and store.get(2) is not a
    a.add_pending(["memex x"])
    assert store.get(2).pending == [] and a.pending == ["memex x"]


def test_pending_sets_have_their_own_token_and_are_capped() -> None:
    from memex.assistant.session import MAX_PENDING_SETS

    s = SessionStore().get(1)
    t1 = s.add_pending(["memex a"])
    t2 = s.add_pending(["memex b", "memex c"])
    assert t1 != t2 and s.pending == ["memex a", "memex b", "memex c"]
    assert s.take_pending(t1) == ["memex a"]  # por token
    assert s.take_pending("nope") is None  # token desconocido
    assert s.take_pending() == ["memex b", "memex c"]  # sin token: la más reciente
    assert s.take_pending() is None and s.pending == []
    for i in range(MAX_PENDING_SETS + 2):
        s.add_pending([f"memex {i}"])
    assert len(s.pending_sets) == MAX_PENDING_SETS and s.pending[0] == "memex 2"
