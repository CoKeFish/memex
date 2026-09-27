"""Estado por chat: historial corto (lo que el modelo ve), escrituras pendientes de confirmación y
un lock para atender un turno a la vez por chat. En memoria: un reinicio del bot lo pierde, lo que
en dev es aceptable (la memoria de verdad es memex, no el chat)."""

from __future__ import annotations

import asyncio
import secrets
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

_YES = frozenset(
    {
        "si",
        "sí",
        "s",
        "ok",
        "okay",
        "dale",
        "confirmo",
        "confirmar",
        "confirma",
        "hazlo",
        "hacelo",
        "listo",
        "va",
        "yes",
        "y",
        "correcto",
        "adelante",
        "ejecuta",
        "ejecutá",
        "ejecutalo",
    }
)
_NO = frozenset({"no", "n", "cancelar", "cancela", "cancelá", "nel", "nop", "olvidalo", "olvídalo"})


def parse_confirmation(text: str) -> Literal["yes", "no"] | None:
    """«sí» / «dale» / «no» / «cancelar» (solo mensajes cortos: «sí, pero…» NO es confirmación)."""
    words = [w.strip(".,!¡¿?") for w in text.strip().lower().split()]
    if not words or len(words) > 2:
        return None
    if all(w in _YES for w in words):
        return "yes"
    if all(w in _NO for w in words):
        return "no"
    return None


#: Cuántas propuestas sin confirmar se recuerdan por chat. Cada una tiene sus botones; la más
#: vieja se descarta cuando entra una nueva de más.
MAX_PENDING_SETS = 5


@dataclass
class PendingSet:
    """Las escrituras propuestas en UN turno, con el token que llevan sus botones. Antes había una
    sola lista por chat y cada turno la pisaba: dos fotos seguidas → el «Confirmar» de la primera
    ejecutaba la segunda y el de la segunda no encontraba nada (2026-09-17)."""

    token: str
    commands: list[str]


@dataclass
class ChatSession:
    chat_id: int
    max_turns: int = 8
    history: deque[tuple[str, str]] = field(default_factory=deque)
    pending_sets: list[PendingSet] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def add(self, role: Literal["usuario", "asistente"], text: str) -> None:
        self.history.append((role, text))
        while len(self.history) > self.max_turns * 2:
            self.history.popleft()

    def snapshot(self) -> list[tuple[str, str]]:
        return list(self.history)

    @property
    def pending(self) -> list[str]:
        """Todo lo propuesto y sin confirmar, de la propuesta más vieja a la más nueva."""
        return [c for ps in self.pending_sets for c in ps.commands]

    def add_pending(self, commands: list[str]) -> str:
        token = secrets.token_hex(3)
        self.pending_sets.append(PendingSet(token, list(commands)))
        while len(self.pending_sets) > MAX_PENDING_SETS:
            self.pending_sets.pop(0)
        return token

    def take_pending(self, token: str | None = None) -> list[str] | None:
        """Saca (y devuelve) la propuesta del `token`; sin token, la más reciente. None si no
        hay."""
        idx = self._index(token)
        if idx is None:
            return None
        return self.pending_sets.pop(idx).commands

    def _index(self, token: str | None) -> int | None:
        if not self.pending_sets:
            return None
        if token is None:
            return len(self.pending_sets) - 1
        for i, ps in enumerate(self.pending_sets):
            if ps.token == token:
                return i
        return None


class SessionStore:
    def __init__(self, *, max_turns: int = 8) -> None:
        self._max_turns = max_turns
        self._sessions: dict[int, ChatSession] = {}

    def get(self, chat_id: int) -> ChatSession:
        session = self._sessions.get(chat_id)
        if session is None:
            session = ChatSession(chat_id, max_turns=self._max_turns)
            self._sessions[chat_id] = session
        return session
