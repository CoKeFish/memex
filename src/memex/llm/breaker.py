"""Corta-circuito por proveedor: al que está sin responder no se le vuelve a preguntar un rato.

El tope de reloj de pared (`LLMConfig.deadline_s`) evita que una llamada clave la corrida, pero no
evita PAGAR la espera una y otra vez: con DeepSeek caído el 2026-09-14 cada mensaje quemaba 180 s
antes de caer a codex, y una corrida de 20 mensajes se volvía una hora de reloj muerto.

Este módulo recuerda, en el proceso, qué proveedor viene fallando. Dos topes seguidos y queda
ABIERTO por `_OPEN_S`: mientras tanto sus llamadas fallan de una (sin red, sin espera) y la cadena
de fallback pasa directo al siguiente. Una sola respuesta buena lo cierra.

Es estado de PROCESO a propósito, no de DB: cada proceso (api, scheduler, asistente) aprende por su
cuenta en segundos y no hay nada que limpiar si se reinicia. No es un registro histórico —para eso
están `llm_calls` y los avisos `llm.<proveedor>.request.deadline`—, es una perilla de ahora mismo.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from memex.logging import get_logger

#: Topes seguidos antes de abrir. Uno solo puede ser mala suerte; dos es un proveedor enfermo.
_FAILS_TO_OPEN = 2

#: Cuánto queda abierto. Suficiente para que una corrida entera no lo toque; corto para que la
#: recuperación del proveedor se note en la corrida siguiente (el job corre cada 10 minutos).
_OPEN_S = 600.0

_log = get_logger("memex.llm.breaker")


@dataclass
class _State:
    fails: int = 0
    open_until: float = 0.0


_states: dict[str, _State] = {}


def seconds_left(key: str, *, now: float | None = None) -> float:
    """Segundos que le quedan al corte, o 0.0 si el proveedor está habilitado."""
    st = _states.get(key)
    if st is None:
        return 0.0
    left = st.open_until - (time.monotonic() if now is None else now)
    return left if left > 0 else 0.0


def record_failure(key: str, *, now: float | None = None) -> float:
    """Anota un tope cumplido. Devuelve los segundos de corte (0.0 si todavía no abre)."""
    st = _states.setdefault(key, _State())
    st.fails += 1
    if st.fails < _FAILS_TO_OPEN:
        return 0.0
    st.open_until = (time.monotonic() if now is None else now) + _OPEN_S
    _log.warning("llm.breaker.open", provider=key, fails=st.fails, open_s=_OPEN_S)
    return _OPEN_S


def record_success(key: str) -> None:
    """Una respuesta buena borra la racha (y el corte, si lo había)."""
    st = _states.get(key)
    if st is None:
        return
    if st.open_until:
        _log.info("llm.breaker.closed", provider=key)
    _states.pop(key, None)


def reset() -> None:
    """Olvida todo — para los tests y para un `reset` manual desde una consola."""
    _states.clear()
