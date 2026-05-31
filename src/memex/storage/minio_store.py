"""MinioObjectStore — el ÚNICO lugar que habla con el backend S3-compatible (MinIO).

Aísla a boto3 detrás del Protocol `ObjectStore`: los callers consumen `bytes` y keys, nunca
shapes de S3. Cambiar a S3 real o a otro backend = otra clase que implementa `ObjectStore`.

boto3/botocore no traen `py.typed` (sus tipos son dinámicos) → se aceptan como `Any` en los
puntos de contacto vía override de mypy (mismo trato que `telethon.*`), manteniendo tipadas
nuestras firmas. El cliente boto3 es inyectable para tests (un fake con la misma forma).
"""

from __future__ import annotations

from typing import Any

import boto3
from botocore.exceptions import ClientError

from memex.logging import get_logger
from memex.storage.client import (
    ObjectStore,
    StorageAccessError,
    StorageError,
    StorageRegionError,
)
from memex.storage.config import StorageConfig

# Clasificación de errores de S3/MinIO. En operaciones HEAD (head_bucket/head_object) no hay body
# XML, así que botocore deja `Error.Code` como el status HTTP en string ("404"/"403"/"301") y el
# status numérico en `ResponseMetadata.HTTPStatusCode`. Miramos ambos: el status HTTP es la señal
# robusta para HEAD; los códigos simbólicos cubren operaciones con body y variantes de MinIO.
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})
_FORBIDDEN_CODES = frozenset({"403", "AccessDenied", "Forbidden"})
_REDIRECT_CODES = frozenset({"301", "PermanentRedirect", "MovedPermanently"})


def _error_code(exc: ClientError) -> str:
    response: dict[str, Any] = getattr(exc, "response", {}) or {}
    error: dict[str, Any] = response.get("Error", {}) or {}
    return str(error.get("Code", ""))


def _http_status(exc: ClientError) -> int | None:
    response: dict[str, Any] = getattr(exc, "response", {}) or {}
    meta: dict[str, Any] = response.get("ResponseMetadata", {}) or {}
    status = meta.get("HTTPStatusCode")
    return status if isinstance(status, int) else None


def _bucket_region(exc: ClientError) -> str | None:
    """Región real del bucket en un redirect 301 (header `x-amz-bucket-region`, en minúsculas)."""
    response: dict[str, Any] = getattr(exc, "response", {}) or {}
    meta: dict[str, Any] = response.get("ResponseMetadata", {}) or {}
    headers: dict[str, Any] = meta.get("HTTPHeaders", {}) or {}
    region = headers.get("x-amz-bucket-region")
    return str(region) if region else None


def _region_detail(exc: ClientError) -> str:
    region = _bucket_region(exc)
    return f" (el bucket está en la región {region!r})" if region else ""


def _is_not_found(exc: ClientError) -> bool:
    return _http_status(exc) == 404 or _error_code(exc) in _NOT_FOUND_CODES


def _is_forbidden(exc: ClientError) -> bool:
    return _http_status(exc) == 403 or _error_code(exc) in _FORBIDDEN_CODES


def _is_redirect(exc: ClientError) -> bool:
    return _http_status(exc) == 301 or _error_code(exc) in _REDIRECT_CODES


class MinioObjectStore:
    """`ObjectStore` sobre MinIO/S3 vía boto3 (síncrono).

    Implementa el Protocol `ObjectStore`. Construir con `client` inyectado para tests, o dejar
    que cree el suyo desde la `StorageConfig`.
    """

    def __init__(self, config: StorageConfig, *, client: Any | None = None) -> None:
        self._bucket = config.bucket
        self._log = get_logger("memex.storage.minio")
        self._client: Any = client or boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key.get_secret_value(),
            aws_secret_access_key=config.secret_key.get_secret_value(),
            region_name=config.region,
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as e:
            if _is_not_found(e):
                self._client.create_bucket(Bucket=self._bucket)
                self._log.info("storage.bucket.created", bucket=self._bucket)
            elif _is_forbidden(e):
                raise StorageAccessError(
                    f"head_bucket {self._bucket!r} denegado (403): el bucket existe pero la "
                    f"credencial/policy no tiene acceso: {e}"
                ) from e
            elif _is_redirect(e):
                raise StorageRegionError(
                    f"head_bucket {self._bucket!r} redirigido (301): el bucket vive en otra "
                    f"región que la configurada{_region_detail(e)}: {e}"
                ) from e
            else:
                raise StorageError(f"head_bucket {self._bucket!r} failed: {e}") from e

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
            )
        except ClientError as e:
            raise StorageError(f"put_object {key!r} failed: {e}") from e

    def get(self, key: str) -> bytes:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            raise StorageError(f"get_object {key!r} failed: {e}") from e
        data: bytes = resp["Body"].read()
        return data

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            if _is_not_found(e):
                return False
            if _is_forbidden(e):
                raise StorageAccessError(
                    f"head_object {key!r} denegado (403): el objeto podría existir pero la "
                    f"credencial/policy no tiene acceso: {e}"
                ) from e
            if _is_redirect(e):
                raise StorageRegionError(
                    f"head_object {key!r} redirigido (301): el bucket {self._bucket!r} vive en "
                    f"otra región que la configurada{_region_detail(e)}: {e}"
                ) from e
            raise StorageError(f"head_object {key!r} failed: {e}") from e
        return True


def _assert_protocol() -> None:
    # Falla en mypy si MinioObjectStore deja de satisfacer ObjectStore.
    _: type[ObjectStore] = MinioObjectStore
