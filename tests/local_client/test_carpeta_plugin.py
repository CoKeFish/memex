"""Plugin `carpeta`: los documentos que el usuario deja en una carpeta entran a memex.

Lo que importa de este camino: no duplicar (el mismo archivo dejado dos veces, o renombrado, es el
MISMO documento), no ensuciar el inbox con lo que memex no sabe leer, y no tocar los archivos del
usuario.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from memex.core.source import Source, SourceKind, SourceRecord


def _load_plugin() -> ModuleType:
    """Carga el plugin desde `plugins/` como hace el cliente local (y `test_plugin_contracts`):
    no es un paquete instalado, así que se abre por ruta."""
    path = Path(__file__).resolve().parents[2] / "plugins" / "carpeta" / "__init__.py"
    spec = importlib.util.spec_from_file_location("_test_carpeta", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_test_carpeta"] = mod
    spec.loader.exec_module(mod)
    return mod


carpeta = _load_plugin()

PDF_BYTES = b"%PDF-1.4 extracto de agosto"


def _folder(tmp_path: Path, **files: bytes) -> Path:
    for filename, data in files.items():
        (tmp_path / filename.replace("__", ".")).write_bytes(data)
    return tmp_path


def _source(folder: Path, **cfg: object) -> Any:
    src = carpeta.build_source({"folder": str(folder), **cfg})
    assert isinstance(src, Source)
    return src


def test_emite_un_registro_por_documento_con_sus_bytes(tmp_path: Path) -> None:
    _folder(tmp_path, extracto_agosto__pdf=PDF_BYTES)
    records = list(_source(tmp_path).fetch(carpeta.CarpetaCursor()))

    assert len(records) == 1
    rec = records[0]
    assert rec.payload["filename"] == "extracto_agosto.pdf"
    assert rec.payload["content_type"] == "application/pdf"
    assert rec.payload["size"] == len(PDF_BYTES)
    # Los BYTES viajan: sin esto el OCR no tendría qué leer y el documento sería solo un nombre.
    assert len(rec.media) == 1 and rec.media[0].content_type == "application/pdf"
    assert rec.media[0].size == len(PDF_BYTES)


def test_el_mismo_archivo_no_se_duplica_aunque_se_renombre(tmp_path: Path) -> None:
    """La identidad es el CONTENIDO: el `external_id` es su sha256."""
    _folder(tmp_path, uno__pdf=PDF_BYTES, copia__pdf=PDF_BYTES)
    ids = {r.external_id for r in _source(tmp_path).fetch(carpeta.CarpetaCursor())}
    assert len(ids) == 1
    # Mismo prefijo que las otras vías de entrega: un documento que ya entró por el bot o por la
    # web no vuelve a entrar por la carpeta.
    assert next(iter(ids)).startswith("documento:")


def test_ignora_lo_que_memex_no_sabe_leer(tmp_path: Path) -> None:
    """La carpeta es del usuario: puede tener de todo sin ensuciar el inbox."""
    _folder(
        tmp_path,
        bueno__pdf=PDF_BYTES,
        hoja__xlsx=b"no soportado",
        nota__txt=b"suelta",
        vacio__pdf=b"",
    )
    nombres = [r.payload["filename"] for r in _source(tmp_path).fetch(carpeta.CarpetaCursor())]
    assert nombres == ["bueno.pdf"]  # el vacío tampoco (0 bytes no es un documento)


def test_respeta_el_tope_de_tamano(tmp_path: Path) -> None:
    _folder(tmp_path, enorme__pdf=b"x" * (2 * 1024 * 1024))
    assert list(_source(tmp_path, max_mb=1).fetch(carpeta.CarpetaCursor())) == []
    assert len(list(_source(tmp_path, max_mb=5).fetch(carpeta.CarpetaCursor()))) == 1


def test_subcarpetas_segun_configuracion(tmp_path: Path) -> None:
    sub = tmp_path / "banco"
    sub.mkdir()
    (sub / "extracto.pdf").write_bytes(PDF_BYTES)
    assert len(list(_source(tmp_path).fetch(carpeta.CarpetaCursor()))) == 1
    assert list(_source(tmp_path, recursive=False).fetch(carpeta.CarpetaCursor())) == []


def test_el_cursor_saltea_lo_viejo_pero_toma_lo_nuevo(tmp_path: Path) -> None:
    """El cursor es solo una optimización para no releer la carpeta entera. Deja un margen hacia
    atrás porque un archivo COPIADO conserva la fecha del original; releer de más es inofensivo
    (el sha256 deduplica), perder un documento no."""
    import os

    viejo = tmp_path / "viejo.pdf"
    viejo.write_bytes(b"%PDF viejo")
    hace_un_mes = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    os.utime(viejo, (hace_un_mes, hace_un_mes))
    src = _source(tmp_path)

    # Sin cursor entra todo.
    assert [r.payload["filename"] for r in src.fetch(carpeta.CarpetaCursor())] == ["viejo.pdf"]

    # Con el cursor en HOY, el de hace un mes queda por debajo del margen y no se relee...
    hoy = carpeta.CarpetaCursor(last_mtime=datetime.now(UTC))
    assert list(src.fetch(hoy)) == []

    # ...pero un documento nuevo sí entra.
    (tmp_path / "nuevo.pdf").write_bytes(b"%PDF nuevo")
    assert [r.payload["filename"] for r in src.fetch(hoy)] == ["nuevo.pdf"]


def test_el_cursor_avanza_con_lo_mas_reciente(tmp_path: Path) -> None:
    _folder(tmp_path, doc__pdf=PDF_BYTES)
    src = _source(tmp_path)
    record = next(iter(src.fetch(carpeta.CarpetaCursor())))
    cursor = src.advance_checkpoint(carpeta.CarpetaCursor(), record)
    assert cursor.last_mtime == record.occurred_at


def test_el_cursor_no_retrocede(tmp_path: Path) -> None:
    src = _source(tmp_path)
    reciente = carpeta.CarpetaCursor(last_mtime=datetime.now(UTC))
    viejo = SourceRecord(
        external_id="carpeta:x",
        occurred_at=datetime.now(UTC) - timedelta(days=5),
        payload={},
        dedupe_keys=[],
    )
    assert src.advance_checkpoint(reciente, viejo) == reciente


def test_no_toca_los_archivos_del_usuario(tmp_path: Path) -> None:
    _folder(tmp_path, extracto__pdf=PDF_BYTES)
    antes = sorted(p.name for p in tmp_path.iterdir())
    list(_source(tmp_path).fetch(carpeta.CarpetaCursor()))
    assert sorted(p.name for p in tmp_path.iterdir()) == antes
    assert (tmp_path / "extracto.pdf").read_bytes() == PDF_BYTES


def test_contrato_del_plugin(tmp_path: Path) -> None:
    assert carpeta.source_type == "carpeta"
    src = _source(tmp_path)
    assert src.kind is SourceKind.DOCUMENTOS
    assert carpeta.validate_requirements({"folder": str(tmp_path)}) == []
    faltante = carpeta.validate_requirements({})
    assert faltante and faltante[0].code == "folder_missing"
    inexistente = carpeta.validate_requirements({"folder": str(tmp_path / "no-existe")})
    assert inexistente and inexistente[0].code == "folder_not_found"


@pytest.mark.asyncio
async def test_health_check_mira_la_carpeta(tmp_path: Path) -> None:
    assert (await _source(tmp_path).health_check()).status == "healthy"
    assert (await _source(tmp_path / "fantasma").health_check()).status == "unhealthy"


def test_declara_la_clave_de_contenido_para_cruzar_vias(tmp_path: Path) -> None:
    """La carpeta es UNA fuente más: sin la clave de contenido, un documento que ya entró por el
    bot o por la web entraría otra vez acá."""
    import hashlib

    _folder(tmp_path, extracto__pdf=PDF_BYTES)
    record = next(iter(_source(tmp_path).fetch(carpeta.CarpetaCursor())))
    assert record.dedupe_keys == [f"sha256:{hashlib.sha256(PDF_BYTES).hexdigest()}"]
