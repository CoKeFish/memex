"""Contrato provider-agnóstico de la capa de almacenamiento de objetos.

Define el Protocol `ObjectStore` (la abstracción contra la que tipan los callers) y la
base de errores `StorageError`. Un backend concreto (`MinioObjectStore`) implementa el
Protocol; los callers NUNCA tipan contra la clase concreta, igual que el runner tipa
contra `MemexSink` y la capa LLM contra `LLMClient`.

En la DB va SOLO la referencia (`object_key` + `bucket`), nunca el blob ni el secreto
(ADR-001 / ADR-015 §7). El object-key es content-addressed (`media/{user}/{sha256}{ext}`):
re-subir la misma imagen es un no-op idempotente y dedup-ea por contenido.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class StorageError(Exception):
    """Base de todos los errores de la capa de almacenamiento — los callers la atrapan genérica."""


class StorageAccessError(StorageError):
    """El backend respondió 403: el recurso existe pero la credencial/policy no da acceso.

    Distinto de "no existe" (404): NO se crea el bucket ni se reporta `exists()=False`
    —el objeto/bucket podría estar ahí, sólo que sin permiso—. Revisar las credenciales
    `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` o la policy del bucket. (En S3 un 403 también
    puede tapar un "no existe" cuando faltan permisos de listado; igual no podríamos
    crearlo, así que tratarlo como error de acceso es lo seguro.)
    """


class StorageRegionError(StorageError):
    """El backend respondió 301: el bucket existe pero en otra región que la configurada.

    El cliente apunta a una región y el bucket vive en otra (redirect permanente). Ajustar
    `MEMEX_MINIO_REGION` (o el endpoint) a la región real; cuando el backend la reporta en
    el header `x-amz-bucket-region`, la región se incluye en el mensaje del error.
    """


#: content-type → extensión de archivo para el object-key (solo cosmético/debug; el sha256 es
#: lo que identifica el contenido).
_CONTENT_TYPE_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "application/pdf": ".pdf",
}


def _ext_for_content_type(content_type: str) -> str:
    return _CONTENT_TYPE_EXT.get(content_type.split(";")[0].strip().lower(), "")


def object_key_for(user_id: int, sha256: str, content_type: str) -> str:
    """Object-key content-addressed: `media/{user_id}/{sha256}{ext}`.

    Content-addressed → el mismo blob siempre cae en la misma key (idempotencia del `put`
    + dedup por contenido). Particionado por `user_id` para aislamiento multi-tenant.
    """
    return f"media/{user_id}/{sha256}{_ext_for_content_type(content_type)}"


@runtime_checkable
class ObjectStore(Protocol):
    """Interfaz de almacenamiento de objetos agnóstica del backend (MinIO/S3/...).

    Síncrona: boto3 (el backend concreto) es sync. Los callers async la envuelven con
    `asyncio.to_thread`. Todas las operaciones son por-key; el bucket vive en la config.
    """

    @property
    def bucket(self) -> str:
        """Nombre del bucket donde viven los objetos (se persiste en `media_assets.bucket`)."""
        ...

    def ensure_bucket(self) -> None:
        """Crea el bucket si no existe (idempotente). Llamar una vez al arrancar."""
        ...

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        """Sube `data` bajo `key`. Idempotente para keys content-addressed (mismo blob → no-op)."""
        ...

    def get(self, key: str) -> bytes:
        """Descarga el blob de `key`. Levanta `StorageError` si no existe o falla."""
        ...

    def exists(self, key: str) -> bool:
        """True si `key` existe en el bucket."""
        ...
