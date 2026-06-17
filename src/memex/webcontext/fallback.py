"""FallbackWebContextProvider — cadena de proveedores detrás del Protocol `WebContextProvider`.

Envuelve una lista ORDENADA de `(nombre, proveedor)` y prueba en orden: el primer `search` exitoso
gana. Ante CUALQUIER `WebContextError` de runtime (codex muerto, cuota, no-encontrado, formato
inválido) salta al siguiente proveedor — la idea es conseguir un perfil de ALGUNO. Si todos fallan,
re-levanta el último error. Calca `memex.llm.fallback.FallbackClient`; los callers tipan contra el
Protocol y no se enteran de la cadena.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from memex.logging import get_logger
from memex.webcontext.client import (
    EntityKind,
    ProfileResult,
    WebContextError,
    WebContextProvider,
)


class FallbackWebContextProvider:
    """Proveedor que satisface el Protocol probando una cadena (p. ej. codex→firecrawl)."""

    name: ClassVar[str] = "fallback"

    def __init__(self, providers: Sequence[tuple[str, WebContextProvider]]) -> None:
        if not providers:
            raise ValueError("FallbackWebContextProvider necesita al menos un proveedor")
        self._providers = list(providers)
        self._log = get_logger("memex.webcontext.fallback")

    async def search(self, name: str, kind: EntityKind) -> ProfileResult:
        failures: list[str] = []
        last_exc: WebContextError | None = None
        for prov_name, provider in self._providers:
            try:
                result = await provider.search(name, kind)
            except WebContextError as e:
                self._log.warning(
                    "webcontext.fallback.attempt_failed",
                    provider=prov_name,
                    error_class=type(e).__name__,
                    status_code=e.status_code,
                    error=str(e)[:200],
                    attempt=len(failures) + 1,
                )
                failures.append(prov_name)
                last_exc = e
                continue
            if failures:
                self._log.info(
                    "webcontext.fallback.served", provider=prov_name, prior_failures=failures
                )
            return result

        self._log.error(
            "webcontext.fallback.exhausted",
            providers=[p for p, _ in self._providers],
            failures=failures,
        )
        assert last_exc is not None  # la cadena no es vacía → hubo al menos un intento
        raise last_exc

    async def aclose(self) -> None:
        for _, provider in self._providers:
            await provider.aclose()
