"""MinioObjectStore con un cliente boto3 FAKE (sin red, sin moto).

Verifica el contrato `ObjectStore`: ensure_bucket crea-si-falta, put/get round-trip, exists,
e idempotencia del put, más la distinción de errores no-404 (403 → acceso, 301 → región). El
fake imita la forma de boto3 (incluye `ClientError` real con `Error.Code` + `ResponseMetadata`).
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from memex.storage import (
    MinioObjectStore,
    ObjectStore,
    StorageAccessError,
    StorageConfig,
    StorageError,
    StorageRegionError,
)


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    """Imita el subconjunto de boto3 s3 client que usa MinioObjectStore."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.buckets: set[str] = set()
        self.put_calls = 0

    def head_bucket(self, *, Bucket: str) -> None:
        if Bucket not in self.buckets:
            raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")

    def create_bucket(self, *, Bucket: str) -> None:
        self.buckets.add(Bucket)

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:
        self.put_calls += 1
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.objects[(Bucket, Key)])}

    def head_object(self, *, Bucket: str, Key: str) -> None:
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")


def _store(client: object) -> MinioObjectStore:
    cfg = StorageConfig(
        endpoint_url="http://localhost:9000",
        access_key=SecretStr("a"),
        secret_key=SecretStr("b"),
        bucket="test-bucket",
    )
    return MinioObjectStore(cfg, client=client)


def test_satisfies_protocol() -> None:
    # issubclass no aplica a Protocols con miembros no-método (la property `bucket`);
    # isinstance sí (runtime_checkable verifica presencia de atributos/métodos).
    assert isinstance(_store(FakeS3()), ObjectStore)


def test_ensure_bucket_creates_when_missing() -> None:
    fake = FakeS3()
    _store(fake).ensure_bucket()
    assert "test-bucket" in fake.buckets


def test_put_get_round_trip() -> None:
    fake = FakeS3()
    store = _store(fake)
    store.put("media/1/abc.png", b"hello", content_type="image/png")
    assert store.get("media/1/abc.png") == b"hello"


def test_exists() -> None:
    fake = FakeS3()
    store = _store(fake)
    assert store.exists("missing") is False
    store.put("k", b"x", content_type="image/png")
    assert store.exists("k") is True


def test_put_is_idempotent() -> None:
    fake = FakeS3()
    store = _store(fake)
    store.put("k", b"x", content_type="image/png")
    store.put("k", b"x", content_type="image/png")
    assert store.get("k") == b"x"
    assert fake.put_calls == 2  # cada put llega al backend; content-addressed = mismo Key


def test_bucket_property() -> None:
    assert _store(FakeS3()).bucket == "test-bucket"


# --- Distinción de errores S3/MinIO no-404 (403 → acceso, 301 → región) ---------------------


def _client_error(
    operation: str,
    *,
    code: str = "",
    status: int | None = None,
    region: str | None = None,
) -> ClientError:
    """Construye un `ClientError` con la forma de botocore (Error.Code + ResponseMetadata)."""
    error: dict[str, Any] = {}
    if code:
        error["Code"] = code
    meta: dict[str, Any] = {}
    if status is not None:
        meta["HTTPStatusCode"] = status
    if region is not None:
        meta["HTTPHeaders"] = {"x-amz-bucket-region": region}
    response: dict[str, Any] = {"Error": error}
    if meta:
        response["ResponseMetadata"] = meta
    return ClientError(response, operation)


class _RaisingS3:
    """Fake boto3 que levanta un `ClientError` fijo en head_bucket/head_object."""

    def __init__(self, error: ClientError) -> None:
        self._error = error
        self.created: list[str] = []

    def head_bucket(self, *, Bucket: str) -> None:
        raise self._error

    def head_object(self, *, Bucket: str, Key: str) -> None:
        raise self._error

    def create_bucket(self, *, Bucket: str) -> None:
        self.created.append(Bucket)


def test_ensure_bucket_forbidden_raises_access_error() -> None:
    # 403: el bucket existe pero sin acceso → NO crear, error de acceso distinguible.
    fake = _RaisingS3(_client_error("HeadBucket", code="403", status=403))
    with pytest.raises(StorageAccessError):
        _store(fake).ensure_bucket()
    assert fake.created == []


def test_ensure_bucket_redirect_raises_region_error_with_region() -> None:
    # 301: el bucket vive en otra región → error de región que incluye la región reportada.
    fake = _RaisingS3(_client_error("HeadBucket", code="301", status=301, region="eu-west-1"))
    with pytest.raises(StorageRegionError) as excinfo:
        _store(fake).ensure_bucket()
    assert "eu-west-1" in str(excinfo.value)
    assert fake.created == []


def test_exists_forbidden_raises_instead_of_false() -> None:
    # 403 en head_object: el objeto podría existir → NO mentir con False.
    fake = _RaisingS3(_client_error("HeadObject", code="403", status=403))
    with pytest.raises(StorageAccessError):
        _store(fake).exists("k")


def test_exists_redirect_raises_region_error() -> None:
    fake = _RaisingS3(_client_error("HeadObject", code="301", status=301))
    with pytest.raises(StorageRegionError):
        _store(fake).exists("k")


def test_classifies_by_http_status_when_symbolic_code_absent() -> None:
    # HEAD no trae body XML → a veces sólo viene HTTPStatusCode, sin Error.Code simbólico.
    fake = _RaisingS3(_client_error("HeadBucket", status=404))
    _store(fake).ensure_bucket()
    assert fake.created == ["test-bucket"]


def test_unmapped_error_still_raises_generic_storage_error() -> None:
    # Un error que no es 404/403/301 sigue cayendo en el StorageError genérico.
    fake = _RaisingS3(_client_error("HeadObject", code="500", status=500))
    with pytest.raises(StorageError) as excinfo:
        _store(fake).exists("k")
    assert type(excinfo.value) is StorageError  # genérico, no las subclases 403/301


def test_exists_forbidden_by_symbolic_code_only() -> None:
    # Sólo Error.Code simbólico ("AccessDenied"), sin status → fija la rama de código.
    fake = _RaisingS3(_client_error("HeadObject", code="AccessDenied"))
    with pytest.raises(StorageAccessError):
        _store(fake).exists("k")


def test_exists_forbidden_by_http_status_only() -> None:
    # Sólo HTTPStatusCode=403, sin Error.Code (caso HEAD sin body) → fija la rama de status.
    fake = _RaisingS3(_client_error("HeadObject", status=403))
    with pytest.raises(StorageAccessError):
        _store(fake).exists("k")


def test_exists_redirect_by_symbolic_code_only() -> None:
    # Sólo Error.Code="PermanentRedirect", sin status → fija la rama simbólica del redirect.
    fake = _RaisingS3(_client_error("HeadObject", code="PermanentRedirect"))
    with pytest.raises(StorageRegionError):
        _store(fake).exists("k")


def test_redirect_message_without_region_is_clean() -> None:
    # 301 sin header x-amz-bucket-region: el mensaje sigue claro, sin fragmento de región.
    fake = _RaisingS3(_client_error("HeadBucket", code="301", status=301))
    with pytest.raises(StorageRegionError) as excinfo:
        _store(fake).ensure_bucket()
    msg = str(excinfo.value)
    assert "redirigido (301)" in msg
    assert "está en la región" not in msg
