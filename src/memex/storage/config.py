"""StorageConfig — configuración resuelta para un backend de objetos (MinIO/S3).

Sigue la convención `from_env` de `LLMConfig` / `SocialConfig`: el *nombre* de la env var se
conoce de antemano, el *valor* nunca toca la DB y los secretos se envuelven en `SecretStr`
para que no aparezcan en logs.

Igual que la API key de DeepSeek, las credenciales de MinIO usan nombres canónicos SIN prefijo
`MEMEX_` (`MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`, los que MinIO usa como root user/password) e
inyectadas por Doppler. El endpoint/bucket/region sí son config del despliegue (`MEMEX_MINIO_*`).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, SecretStr

from memex.storage.client import StorageError

_ENDPOINT_ENV = "MEMEX_MINIO_ENDPOINT"
_ACCESS_KEY_ENV = "MINIO_ACCESS_KEY"
_SECRET_KEY_ENV = "MINIO_SECRET_KEY"
_BUCKET_ENV = "MEMEX_MINIO_BUCKET"
_REGION_ENV = "MEMEX_MINIO_REGION"

_DEFAULT_BUCKET = "memex-media"
_DEFAULT_REGION = "us-east-1"


class StorageConfigError(StorageError):
    """Config inválida o falta una env var requerida.

    Subclasea `StorageError` para que los callers atrapen la base genérica.
    """


class StorageConfig(BaseModel):
    """Configuración resuelta para hablar con un backend S3-compatible.

    `access_key` / `secret_key` son `SecretStr` → redactados en repr/logs/dumps. El cliente
    concreto usa `.get_secret_value()` en el borde de boto3.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_url: str
    access_key: SecretStr
    secret_key: SecretStr
    bucket: str = _DEFAULT_BUCKET
    region: str = _DEFAULT_REGION

    def __repr__(self) -> str:
        return (
            "StorageConfig("
            f"endpoint_url={self.endpoint_url!r}, "
            "access_key=<redacted>, secret_key=<redacted>, "
            f"bucket={self.bucket!r}, region={self.region!r})"
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> StorageConfig:
        """Resuelve las env vars y construye una `StorageConfig` validada.

        Levanta `StorageConfigError` si falta el endpoint o alguna credencial.
        """
        env_map: Mapping[str, str] = env if env is not None else os.environ

        endpoint = env_map.get(_ENDPOINT_ENV, "").strip()
        if not endpoint:
            raise StorageConfigError(f"env var {_ENDPOINT_ENV!r} is not set or resolves to empty")
        access = env_map.get(_ACCESS_KEY_ENV, "").strip()
        if not access:
            raise StorageConfigError(f"env var {_ACCESS_KEY_ENV!r} is not set or resolves to empty")
        secret = env_map.get(_SECRET_KEY_ENV, "").strip()
        if not secret:
            raise StorageConfigError(f"env var {_SECRET_KEY_ENV!r} is not set or resolves to empty")

        bucket = env_map.get(_BUCKET_ENV, "").strip() or _DEFAULT_BUCKET
        region = env_map.get(_REGION_ENV, "").strip() or _DEFAULT_REGION

        return cls(
            endpoint_url=endpoint,
            access_key=SecretStr(access),
            secret_key=SecretStr(secret),
            bucket=bucket,
            region=region,
        )
