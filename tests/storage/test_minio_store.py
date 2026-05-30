"""MinioObjectStore con un cliente boto3 FAKE (sin red, sin moto).

Verifica el contrato `ObjectStore`: ensure_bucket crea-si-falta, put/get round-trip, exists,
e idempotencia del put. El fake imita la forma de boto3 (incluye `ClientError` real para el
manejo de not-found).
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from pydantic import SecretStr

from memex.storage import MinioObjectStore, ObjectStore, StorageConfig


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


def _store(fake: FakeS3) -> MinioObjectStore:
    cfg = StorageConfig(
        endpoint_url="http://localhost:9000",
        access_key=SecretStr("a"),
        secret_key=SecretStr("b"),
        bucket="test-bucket",
    )
    return MinioObjectStore(cfg, client=fake)


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
