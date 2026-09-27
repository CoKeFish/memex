"""La palabra del usuario sobre el estado (reopen/close manual o por el agente) vale hasta que
llegue una señal nueva: la regla de inactividad no la deshace y el curador no la cuestiona."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import store
from memex.modules.asuntos.curator import collect_items, run_system_rules
from tests.asuntos._seed import asunto, msg, source


def _old_signal(aid: int, days: int) -> None:
    with connection() as c:
        c.execute(
            text(
                "UPDATE mod_asuntos SET last_signal_at = NOW() - make_interval(days => :d) "
                "WHERE id = :a"
            ),
            {"a": aid, "d": days},
        )


def test_reopen_manual_no_lo_deshace_la_inactividad(conn: Any) -> None:
    src = source()
    aid = asunto("Condonación FONDEX", kind="tramite", status="inactivo")
    m = msg(src, subject="Recordatorio", occurred=datetime.now(UTC) - timedelta(days=130))
    with connection() as c:
        store.add_evidence(c, 1, aid, m, method="llm")
    _old_signal(aid, 130)
    with connection() as c:
        assert store.set_status(c, 1, aid, "abierto", source="manual")
        a = store.get_asunto(c, 1, aid)
        assert a is not None and a.status == "abierto" and a.manual_status_at is not None
        st = run_system_rules(c, 1)
        assert st.inactivated == 0
        assert store.get_asunto(c, 1, aid).status == "abierto"  # type: ignore[union-attr]
        # tampoco entra en el scope «abierto sin señal reciente: ¿terminó?» del curador
        items = collect_items(c, 1, scope="open", limit=100, inactive_days=60)
        assert all(aid not in it.asunto_ids for it in items)


def test_la_regla_vuelve_a_aplicar_con_senal_nueva(conn: Any) -> None:
    src = source()
    aid = asunto("Trámite viejo", kind="tramite", status="abierto")
    with connection() as c:
        store.set_status(c, 1, aid, "cerrado", source="agent")
        store.set_status(c, 1, aid, "abierto", source="agent")
    # llega una señal NUEVA (posterior a la palabra del usuario) y luego pasa el tiempo
    m = msg(src, subject="Nuevo correo", occurred=datetime.now(UTC))
    with connection() as c:
        store.add_evidence(c, 1, aid, m, method="llm")
    _old_signal(aid, 90)
    with connection() as c:
        # la señal «vieja» simulada es posterior al sello manual solo si movemos el sello atrás:
        c.execute(
            text(
                "UPDATE mod_asuntos SET metadata = metadata || jsonb_build_object("
                "'manual_status_at', to_jsonb(NOW() - make_interval(days => 120))) WHERE id = :a"
            ),
            {"a": aid},
        )
        st = run_system_rules(c, 1)
        assert st.inactivated == 1
        assert store.get_asunto(c, 1, aid).status == "inactivo"  # type: ignore[union-attr]


def test_sello_manual_aunque_el_estado_no_cambie(conn: Any) -> None:
    aid = asunto("Ya abierto", kind="tramite", status="abierto")
    with connection() as c:
        assert store.get_asunto(c, 1, aid).manual_status_at is None  # type: ignore[union-attr]
        assert not store.set_status(c, 1, aid, "abierto", source="manual")  # sin cambio de estado
        assert store.get_asunto(c, 1, aid).manual_status_at is not None  # type: ignore[union-attr]
        # una fuente del sistema no sella
        bid = asunto("Otro", kind="tramite", status="inactivo")
        store.set_status(c, 1, bid, "abierto", source="system")
        assert store.get_asunto(c, 1, bid).manual_status_at is None  # type: ignore[union-attr]


def test_settle_after_force_borra_vacios_degrada_delgados_y_respeta_cierres(conn: Any) -> None:
    src = source()
    auto = asunto("Semillero X", kind="comunidad", status="abierto")
    tocado = asunto("Condonación", kind="tramite", status="abierto")
    cand = asunto("Candidato", kind="otro", status="candidato")
    cerrado = asunto("Hackathon pasado", kind="hackathon", status="cerrado")
    fuerte = asunto("Programa", kind="programa", status="abierto", seed="seleccion")
    m1, m2, m3, m4, m5 = (msg(src, subject=f"s{i}", occurred=datetime.now(UTC)) for i in range(5))
    with connection() as c:
        store.add_evidence(c, 1, auto, m1, method="llm")
        store.add_evidence(c, 1, tocado, m2, method="llm")
        store.add_evidence(c, 1, cand, m3, method="llm")
        store.add_evidence(c, 1, cerrado, m4, method="llm")
        store.add_evidence(c, 1, fuerte, m5, method="llm")
        store.set_priority(c, 1, tocado, "alta", source="user")
        touched = store.asuntos_of(c, 1, [m1, m2, m3, m4, m5])
        store.forget(c, 1, [m1, m2, m3, m4, m5])
        # forget: solo borra candidatos vacíos; los demás CONSERVAN su estado durante la corrida
        assert store.get_asunto(c, 1, cand) is None
        assert store.get_asunto(c, 1, auto).status == "abierto"  # type: ignore[union-attr]
        assert store.get_asunto(c, 1, cerrado).status == "cerrado"  # type: ignore[union-attr]
        # la corrida re-elige: un correo para `auto`, uno para `cerrado` y uno para `fuerte`
        store.add_evidence(c, 1, auto, m1, method="llm")
        store.add_evidence(c, 1, cerrado, m4, method="llm")
        store.add_evidence(c, 1, fuerte, m5, method="llm")
        purged, demoted = store.settle_after_force(c, 1, touched, min_evidence_open=2)
        st = {a.id: a.status for a in store.list_asuntos(c, 1)}
    assert (purged, demoted) == (0, 1)
    assert st[auto] == "candidato"  # un solo correo no sostiene un abierto
    assert st[cerrado] == "cerrado"  # el cierre previo se conserva
    assert st[fuerte] == "abierto"  # semilla fuerte: un correo alcanza
    assert tocado in st and st[tocado] == "abierto"  # la palabra del usuario lo conserva (n=0)
