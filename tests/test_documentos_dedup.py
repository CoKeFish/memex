"""El mismo documento entregado por VARIAS vías entra una sola vez.

Cada vía de carga (bot de Telegram, dashboard, consola, carpeta vigilada) tiene su propia FUENTE,
así que el `UNIQUE(source_id, external_id)` no las cruza: sin una clave de contenido, mandarle el
mismo extracto al bot y subirlo además por la web creaba DOS registros y duplicaba cada movimiento
en finanzas. La clave es el sha256 del archivo: igualdad exacta de bytes, sin falso positivo
posible.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest
from sqlalchemy import text

from memex.api.object_store import set_object_store
from memex.db import connection
from memex.documentos.ingest import entregar_bytes

PDF = b"%PDF-1.4 extracto de agosto"
SHA = hashlib.sha256(PDF).hexdigest()
#: Las vías reales de entrega. Cada una crea su propia fuente `documentos-<canal>`.
CANALES = ["telegram", "web", "consola", "carpeta"]


class _FakeStore:
    bucket = "memex-media"

    def __init__(self) -> None:
        self.puts: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:
        pass

    def put(self, key: str, data: bytes, *, content_type: str) -> None:
        self.puts[key] = data

    def get(self, key: str) -> bytes:
        return self.puts[key]

    def exists(self, key: str) -> bool:
        return key in self.puts


@pytest.fixture(autouse=True)
def _store() -> Iterator[_FakeStore]:
    store = _FakeStore()
    set_object_store(store)
    yield store
    set_object_store(None)


def _entregar(canal: str, *, data: bytes = PDF, filename: str = "extracto.pdf") -> bool:
    """Entrega por un canal; devuelve si quedó como duplicado."""
    with connection() as conn:
        return entregar_bytes(conn, 1, data=data, filename=filename, canal=canal).duplicado


def _filas() -> int:
    with connection() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM inbox WHERE external_id = :e"),
                {"e": f"documento:{SHA}"},
            ).scalar_one()
        )


def test_el_mismo_archivo_por_cada_via_entra_una_sola_vez() -> None:
    duplicados = [_entregar(canal) for canal in CANALES]

    assert duplicados == [False, True, True, True], "solo la primera vía debería insertar"
    assert _filas() == 1, "el mismo documento quedó más de una vez en el inbox"


def test_da_igual_el_orden_de_las_vias() -> None:
    """Ninguna vía es privilegiada: la primera que llegue gana, las demás deduplican."""
    for canal in reversed(CANALES):
        duplicado = _entregar(canal)
        assert duplicado is (canal != CANALES[-1])
    assert _filas() == 1


def test_tambien_dedup_si_cambia_el_nombre_del_archivo() -> None:
    """Renombrar no lo hace otro documento: la identidad es el contenido."""
    assert _entregar("web", filename="agosto.pdf") is False
    assert _entregar("telegram", filename="extracto-agosto-final(2).pdf") is True
    assert _filas() == 1


def test_un_archivo_distinto_no_se_confunde() -> None:
    """El gate no puede tragarse documentos diferentes: sería perder datos."""
    assert _entregar("web") is False
    assert _entregar("web", data=b"%PDF-1.4 otro extracto", filename="julio.pdf") is False
    with connection() as conn:
        distintos = int(
            conn.execute(
                text("SELECT count(*) FROM inbox WHERE external_id LIKE 'documento:%'")
            ).scalar_one()
        )
    assert distintos == 2


def test_la_clave_de_contenido_queda_registrada() -> None:
    """Es lo que hace posible el cruce entre fuentes; si no se escribe, el dedup no existe."""
    _entregar("consola")
    with connection() as conn:
        clave = conn.execute(
            text("SELECT key FROM inbox_dedupe_keys WHERE user_id = 1 AND key = :k"),
            {"k": f"sha256:{SHA}"},
        ).scalar()
    assert clave == f"sha256:{SHA}"
