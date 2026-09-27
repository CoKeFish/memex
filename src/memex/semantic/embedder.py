"""Quién calcula los vectores.

- `LocalEmbedder`: fastembed (ONNX en CPU), modelo multilingüe chico. Carga perezosa y una sola vez
  por proceso; sin red en runtime (el modelo se hornea en la imagen, `FASTEMBED_CACHE_PATH`).
  Medido en el PC del dueño: ~1,4 ms por título; carga ~8 s la primera vez.
- `FixedEmbedder`: doble para tests: vectores dados por texto (o por prefijo), sin modelo.
- `get_embedder()`: el que dicta `settings.semantic_provider` (`local` | `off`); `off` desactiva el
  bloqueo semántico sin tocar código. Los tests inyectan uno con `use_embedder`.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Protocol

from memex.config import settings
from memex.logging import get_logger

_log = get_logger("memex.semantic.embedder")


class Embedder(Protocol):
    #: Nombre estable del modelo: llave de los vectores guardados (cambiar de modelo = re-embeber).
    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def normalize(vec: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    return [x / n for x in vec] if n > 0 else [0.0 for _ in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Coseno de dos vectores YA normalizados (los guardados lo están)."""
    return float(sum(x * y for x, y in zip(a, b, strict=False)))


class FixedEmbedder:
    """Doble de test: `vectors` mapea texto (normalizado a minúsculas) → vector. Un texto sin
    vector recibe uno determinista y ortogonal a todo lo demás (no se parece a nada)."""

    model = "fixed-test"

    def __init__(self, vectors: Mapping[str, Sequence[float]] | None = None, dim: int = 8) -> None:
        self.vectors = {k.lower().strip(): normalize(v) for k, v in (vectors or {}).items()}
        self.dim = dim
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        out: list[list[float]] = []
        for t in texts:
            key = t.lower().strip()
            vec = self.vectors.get(key)
            if vec is None:
                # ortogonal determinista: un 1 en una posición derivada del texto
                dim = max(self.dim, max((len(v) for v in self.vectors.values()), default=0))
                vec = [0.0] * dim
                vec[hash(key) % dim] = 1.0
            out.append(list(vec))
        return out


class LocalEmbedder:
    """fastembed (ONNX, CPU). Carga perezosa y compartida por proceso."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._impl: object | None = None
        self._lock = threading.Lock()

    def _load(self) -> object:
        if self._impl is None:
            with self._lock:
                if self._impl is None:
                    from fastembed import TextEmbedding

                    _log.info("semantic.model.loading", model=self.model)
                    self._impl = TextEmbedding(model_name=self.model)
                    _log.info("semantic.model.loaded", model=self.model)
        return self._impl

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        impl = self._load()
        vecs = impl.embed(list(texts), batch_size=64)  # type: ignore[attr-defined]
        return [normalize([float(x) for x in v]) for v in vecs]


_current: Embedder | None = None
_override: Embedder | None = None
#: `use_embedder(None)` significa APAGADO durante el bloque (no «sin override»).
_override_set = False
_lock = threading.Lock()


def get_embedder() -> Embedder | None:
    """El embedder vigente, o None si el bloqueo semántico está apagado (`semantic_provider=off`)
    o el proveedor no está disponible (falta la dependencia): el resto del sistema sigue sin él."""
    global _current
    if _override_set:
        return _override
    provider = settings.semantic_provider.strip().lower()
    if provider in ("", "off", "none", "0", "false"):
        return None
    if _current is None:
        with _lock:
            if _current is None:
                try:
                    import fastembed  # noqa: F401
                except ImportError:
                    _log.warning("semantic.provider.unavailable", provider=provider)
                    return None
                _current = LocalEmbedder(settings.semantic_model)
    return _current


@contextmanager
def use_embedder(embedder: Embedder | None) -> Iterator[None]:
    """Tests: fija el embedder (o None = apagado) durante el bloque."""
    global _override, _override_set
    prev, prev_set = _override, _override_set
    _override, _override_set = embedder, True
    try:
        yield
    finally:
        _override, _override_set = prev, prev_set
