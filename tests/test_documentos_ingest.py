"""Entregar un archivo a memex (por consola, por Telegram o por carpeta: el mismo camino).

Lo que importa: que los BYTES entren (sin eso el OCR no tiene qué leer), que el mismo documento no
se duplique por mandarlo dos veces, y que lo que memex no sabe leer se rechace con un motivo claro
en vez de entrar y morir después.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import text

from memex.api.object_store import set_object_store
from memex.db import connection
from memex.documentos.ingest import (
    MAX_MB,
    DocumentoError,
    Entrega,
    content_type_of,
    ensure_source,
    entregar_archivo,
    entregar_bytes,
)

PDF = b"%PDF-1.4 extracto"


class _FakeStore:
    """Almacén en memoria: los tests no necesitan MinIO para comprobar que el blob se guarda."""

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


def _entregar(data: bytes = PDF, filename: str = "extracto.pdf", **kw: object) -> Entrega:
    with connection() as conn:
        return entregar_bytes(conn, 1, data=data, filename=filename, canal="test", **kw)  # type: ignore[arg-type]


def test_el_documento_entra_con_sus_bytes() -> None:
    entrega = _entregar()
    assert entrega.inbox_id is not None and not entrega.duplicado
    with connection() as conn:
        row = conn.execute(
            text(
                "SELECT i.payload->>'filename', i.payload->>'captured_from', "
                "m.filename, m.size_bytes "
                "FROM inbox i JOIN media_assets m ON m.inbox_id = i.id WHERE i.id = :id"
            ),
            {"id": entrega.inbox_id},
        ).one()
    assert row[0] == "extracto.pdf" and row[1] == "test"
    # El blob quedó guardado: es lo único que hace legible al documento.
    assert row[2] == "extracto.pdf" and int(row[3]) == len(PDF)


def test_el_mismo_documento_no_se_duplica_aunque_cambie_de_nombre() -> None:
    primero = _entregar(filename="agosto.pdf")
    repetido = _entregar(filename="otro-nombre.pdf")
    assert primero.sha256 == repetido.sha256
    assert repetido.duplicado  # se reconoce por CONTENIDO, no por nombre
    with connection() as conn:
        cuantos = conn.execute(
            text("SELECT count(*) FROM inbox WHERE external_id = :e"),
            {"e": f"documento:{primero.sha256}"},
        ).scalar_one()
    assert cuantos == 1  # una sola fila, no dos


def test_rechaza_lo_que_no_sabe_leer_con_un_motivo() -> None:
    with pytest.raises(DocumentoError, match="tipo no soportado"):
        _entregar(filename="hoja.xlsx")
    with pytest.raises(DocumentoError, match="vacío"):
        _entregar(data=b"", filename="vacio.pdf")
    with pytest.raises(DocumentoError, match=f"{MAX_MB} MB"):
        _entregar(data=b"x" * (MAX_MB * 1024 * 1024 + 1))


def test_content_type_por_extension() -> None:
    assert content_type_of("extracto.PDF") == "application/pdf"
    assert content_type_of("foto.jpeg") == "image/jpeg"
    assert content_type_of("libro.epub") is None


def test_una_fuente_por_canal_creada_al_vuelo() -> None:
    """Cada canal de entrega es su propia fuente: se ve por dónde entró y se puede apagar solo."""
    with connection() as conn:
        telegram = ensure_source(conn, 1, canal="telegram")
        consola = ensure_source(conn, 1, canal="consola")
        otra_vez = ensure_source(conn, 1, canal="telegram")
        tipo = conn.execute(
            text("SELECT type FROM sources WHERE id = :id"), {"id": telegram}
        ).scalar_one()
    assert telegram != consola
    assert otra_vez == telegram  # idempotente
    assert tipo == "documentos"


def test_entregar_un_archivo_del_disco(tmp_path: Path) -> None:
    path = tmp_path / "recibo.pdf"
    path.write_bytes(PDF)
    with connection() as conn:
        entrega = entregar_archivo(conn, 1, path, canal="consola", nota="el de agosto")
    assert entrega.inbox_id is not None
    with connection() as conn:
        nota = conn.execute(
            text("SELECT payload->>'body_text' FROM inbox WHERE id = :id"), {"id": entrega.inbox_id}
        ).scalar_one()
    assert nota == "el de agosto"


def test_archivo_inexistente(tmp_path: Path) -> None:
    with connection() as conn, pytest.raises(DocumentoError, match="no existe"):
        entregar_archivo(conn, 1, tmp_path / "fantasma.pdf")
