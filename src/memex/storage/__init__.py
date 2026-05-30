"""Capa de almacenamiento de objetos de memex (MinIO/S3), provider-agnóstica.

API pública: tipá tus callers contra el Protocol `ObjectStore` y construí el concreto
(`MinioObjectStore`) en el borde. En la DB va solo la referencia (object-key); el blob vive
en MinIO. El object-key es content-addressed (`object_key_for`).

Uso típico:

    from memex.storage import MinioObjectStore, StorageConfig, object_key_for

    store = MinioObjectStore(StorageConfig.from_env())
    store.ensure_bucket()
    key = object_key_for(user_id=1, sha256=sha, content_type="image/png")
    store.put(key, blob, content_type="image/png")
"""

from memex.storage.client import ObjectStore, StorageError, object_key_for
from memex.storage.config import StorageConfig, StorageConfigError
from memex.storage.minio_store import MinioObjectStore

__all__ = [
    "MinioObjectStore",
    "ObjectStore",
    "StorageConfig",
    "StorageConfigError",
    "StorageError",
    "object_key_for",
]
