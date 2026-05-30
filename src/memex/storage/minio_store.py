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
from memex.storage.client import ObjectStore, StorageError
from memex.storage.config import StorageConfig

# Códigos S3/MinIO que significan "no existe" (variantes según operación/backend).
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NoSuchBucket", "NotFound"})


def _error_code(exc: ClientError) -> str:
    response: dict[str, Any] = getattr(exc, "response", {}) or {}
    error: dict[str, Any] = response.get("Error", {}) or {}
    return str(error.get("Code", ""))


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
            if _error_code(e) in _NOT_FOUND_CODES:
                self._client.create_bucket(Bucket=self._bucket)
                self._log.info("storage.bucket.created", bucket=self._bucket)
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
            if _error_code(e) in _NOT_FOUND_CODES:
                return False
            raise StorageError(f"head_object {key!r} failed: {e}") from e
        return True


def _assert_protocol() -> None:
    # Falla en mypy si MinioObjectStore deja de satisfacer ObjectStore.
    _: type[ObjectStore] = MinioObjectStore
