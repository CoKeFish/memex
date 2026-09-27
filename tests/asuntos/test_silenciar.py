"""Silenciar un asunto: queda cerrado aunque siga llegando señal, y no nace un duplicado.

El caso: un NAS repite cada tanto que su certificado caducó y que hubo un apagado anormal. El dueño
no puede hacer nada y quiere dejar de verlo, pero sin tocar la regla general (los avisos de ese
aparato SÍ son relevantes). Cerrarlo a secas no alcanzaba, y fallaba de dos maneras opuestas: el
curador lo reabría con la señal siguiente, y pasados los 60 días de la ventana del catálogo el aviso
número veinte abría un asunto NUEVO del mismo tema.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import store
from tests.asuntos._seed import asunto


def _pendientes(aid: int) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "SELECT count(*) FROM mod_tareas WHERE asunto_id = :a AND status = 'pendiente'"
                ),
                {"a": aid},
            ).scalar_one()
        )


def _tarea(aid: int, title: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_tareas (user_id, title, description, status, asunto_id) "
                    "VALUES (1, :t, '', 'pendiente', :a) RETURNING id"
                ),
                {"t": title, "a": aid},
            ).scalar_one()
        )


def _viejo(aid: int, days: int) -> None:
    """Empuja la actividad al pasado para sacarlo de la ventana del catálogo."""
    with connection() as c:
        c.execute(
            text(
                "UPDATE mod_asuntos SET first_signal_at = NOW() - make_interval(days => :d), "
                "last_signal_at = NOW() - make_interval(days => :d), "
                "closed_at = NOW() - make_interval(days => :d) WHERE id = :a"
            ),
            {"a": aid, "d": days},
        )


def test_silenciar_cierra_y_cancela_lo_pendiente(conn: Any) -> None:
    aid = asunto("TerraMaster TNAS F2-425", kind="servicio")
    _tarea(aid, "Actualizar el certificado")
    _tarea(aid, "Verificar integridad de datos")
    assert _pendientes(aid) == 2
    with connection() as c:
        ok, canceladas = store.silence(c, 1, aid)
    assert ok and canceladas == 2
    with connection() as c:
        a = store.get_asunto(c, 1, aid)
    assert a is not None and a.status == "cerrado" and a.silenced is True
    assert _pendientes(aid) == 0, "un asunto que no vuelve no deja tareas vencidas colgando"


def test_nadie_lo_reabre_salvo_el_dueno(conn: Any) -> None:
    aid = asunto("TNAS", kind="servicio")
    with connection() as c:
        store.silence(c, 1, aid)
        # el curador, el mantenimiento y la promoción pasan todos por acá
        assert store.set_status(c, 1, aid, "abierto", source="curator") is False
        assert store.set_status(c, 1, aid, "abierto", source="system") is False
        assert store.get_asunto(c, 1, aid).status == "cerrado"  # type: ignore[union-attr]
        # el dueño sí, y eso levanta el silencio
        assert store.set_status(c, 1, aid, "abierto", source="manual") is True
        a = store.get_asunto(c, 1, aid)
    assert a is not None and a.status == "abierto" and a.silenced is False


def test_sigue_en_el_catalogo_para_siempre_asi_no_nace_un_duplicado(conn: Any) -> None:
    """Este es el otro miedo: si el silenciado saliera del catálogo, el asignador no lo vería y
    abriría un asunto nuevo del mismo tema."""
    silenciado = asunto("TNAS silenciado", kind="servicio")
    cerrado = asunto("Otro cerrado", kind="servicio")
    with connection() as c:
        store.silence(c, 1, silenciado)
        store.record_state(c, 1, cerrado, "listo", closed=True, source="manual")
    _viejo(silenciado, 400)
    _viejo(cerrado, 400)
    with connection() as c:
        ids = {a.id for a in store.catalog(c, 1, closed_window_days=60)}
    assert silenciado in ids, "el silenciado no puede salir del catálogo"
    assert cerrado not in ids, "el cerrado común sí sale por la ventana"


def test_el_catalogo_lo_muestra_marcado(conn: Any) -> None:
    from memex.modules.asuntos.assign import render_catalog

    aid = asunto("TNAS", kind="servicio")
    with connection() as c:
        store.silence(c, 1, aid)
        cat = store.catalog(c, 1, closed_window_days=60)
    texto = render_catalog([a for a in cat if a.id == aid], datetime.now(UTC).date())
    assert "[SILENCIADO]" in texto and "[CERRADO]" not in texto


def test_levantar_el_silencio_no_lo_reabre_solo(conn: Any) -> None:
    aid = asunto("TNAS", kind="servicio")
    with connection() as c:
        store.silence(c, 1, aid)
        assert store.unsilence(c, 1, aid) is True
        a = store.get_asunto(c, 1, aid)
        assert a is not None and a.silenced is False and a.status == "cerrado"
        # ahora sí puede revivir con una señal nueva
        assert store.set_status(c, 1, aid, "abierto", source="curator") is True
