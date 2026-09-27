"""`origen.py`: observadores por mensaje y etiquetas para el juez (sin DB)."""

from __future__ import annotations

from memex.modules.finance.dedup import KIND_AGENT
from memex.modules.finance.origen import (
    kind_of,
    label_for_observers,
    observer_for,
    observers_for_origin,
)


def test_observer_for_email_is_sender_domain_lowercased() -> None:
    assert observer_for("imap", "Alertas@Banco.com") == "email:banco.com"
    assert observer_for("outlook", "x@tienda.com") == "email:tienda.com"
    assert observer_for("imap", "") == "email"  # sin remitente: solo la categoría
    assert observer_for("imap", None) == "email"


def test_observer_for_chat_documents_and_unknown() -> None:
    assert observer_for("telegram", "-100123") == "chat:-100123"
    assert observer_for("documentos", "lo que sea") == "documentos"  # sin emisor conocido
    assert observer_for("carpeta", "") == "documentos"
    assert observer_for("tipo-inexistente", "") is None


def test_observers_for_origin_parses_hints_and_agent() -> None:
    hints = ["imap\tpagos@pasarela.com", "documentos\t", "raro\t"]
    assert observers_for_origin(hints, [1, 2]) == frozenset({"email:pasarela.com", "documentos"})
    assert observers_for_origin(None, []) == frozenset({KIND_AGENT})
    assert kind_of("email:banco.com") == "email"


def test_labels_for_the_judge() -> None:
    assert label_for_observers({"email:banco.com"}) == "correo de banco.com"
    assert label_for_observers({"email"}) == "correo"
    assert label_for_observers({"documentos"}) == "documento aportado (p. ej. extracto bancario)"
    assert label_for_observers({KIND_AGENT}) == "registro del agente"
    assert label_for_observers({"email:a.com", "chat:1"}) == "chat + correo de a.com"
