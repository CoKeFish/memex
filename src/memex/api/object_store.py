"""Accessor del `ObjectStore` para el server-side de ingest.

El borde de ingest sube los blobs de imágenes a MinIO. Construir el cliente es lazy y memoizado:
solo se crea (leyendo `StorageConfig.from_env`) la PRIMERA vez que un request trae media. Así los
requests sin media —el caso común y casi todos los tests— nunca tocan la config de MinIO.

`set_object_store` es el hook de inyección: los tests le pasan un fake; el arranque del server
podría inyectar uno pre-construido. Resetear con `set_object_store(None)`.
"""

from __future__ import annotations

from memex.storage import MinioObjectStore, ObjectStore, StorageConfig

_store: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    """Devuelve el `ObjectStore` (lo construye lazy desde env la primera vez).

    Llama `ensure_bucket()` al construirlo: MinIO no auto-crea buckets, así el primer `put`
    no falla. Es idempotente y corre una sola vez por proceso.
    """
    global _store
    if _store is None:
        store = MinioObjectStore(StorageConfig.from_env())
        store.ensure_bucket()
        _store = store
    return _store


def set_object_store(store: ObjectStore | None) -> None:
    """Inyecta (tests) o resetea (None) el `ObjectStore` global."""
    global _store
    _store = store
