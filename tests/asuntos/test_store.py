from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import datos, store
from tests.asuntos._seed import asunto, evidence, msg, source


def _vertex_exists(aid: int) -> bool:
    from memex.relations.vertices import list_vertices

    with connection() as c:
        return any(v.id == aid for v in list_vertices(c, 1, slugs=("asunto",)))


def test_candidato_sin_vertice_y_promocion_por_evidencia(conn: Any) -> None:
    src = source()
    aid = asunto("Impulsa Bootcamp", status="candidato")
    assert not _vertex_exists(aid)
    m1, m2 = msg(src, subject="a"), msg(src, subject="b")
    evidence(aid, m1)
    with connection() as c:
        assert store.get_asunto(c, 1, aid).evidence_count == 1  # type: ignore[union-attr]
        assert not store.promote_if_ready(c, 1, aid, min_evidence_open=2)
    evidence(aid, m2, hito="Selección")
    with connection() as c:
        assert store.promote_if_ready(c, 1, aid, min_evidence_open=2)
        a = store.get_asunto(c, 1, aid)
    assert a is not None and a.status == "abierto" and a.evidence_count == 2
    assert _vertex_exists(aid)


def test_semilla_fuerte_promueve_con_una_evidencia(conn: Any) -> None:
    src = source()
    aid = asunto("Dev3Kit", status="candidato", seed="seleccion")
    evidence(aid, msg(src, subject="aceptado"))
    with connection() as c:
        assert store.promote_if_ready(c, 1, aid, min_evidence_open=2)
        assert store.get_asunto(c, 1, aid).status == "abierto"  # type: ignore[union-attr]


def test_evidencia_idempotente_y_hito_se_completa(conn: Any) -> None:
    src = source()
    aid = asunto("X")
    m = msg(src, subject="a")
    with connection() as c:
        assert store.add_evidence(c, 1, aid, m, method="llm") is True
        assert store.add_evidence(c, 1, aid, m, method="llm", hito_text="Inicio") is False
        ev = store.evidence_of(c, 1, aid)
    assert len(ev) == 1 and ev[0].hito_text == "Inicio"


def test_estado_cierra_y_rename_conserva_alias(conn: Any) -> None:
    aid = asunto("GIVE Colombia 2026")
    with connection() as c:
        store.record_state(c, 1, aid, "falta inglés", closed=False, source="llm")
        store.rename(c, 1, aid, "Impact Studio Medellín 2026")
        store.record_state(c, 1, aid, "completado", closed=True, source="llm")
        a = store.get_asunto(c, 1, aid)
        states = store.states_of(c, 1, aid)
    assert a is not None
    assert a.name == "Impact Studio Medellín 2026" and "GIVE Colombia 2026" in a.aliases
    assert a.status == "cerrado" and a.state_text == "completado" and a.closed_at is not None
    assert [s.closed for s in states] == [False, True]


def test_estado_vigente_es_el_del_mensaje_mas_reciente(conn: Any) -> None:
    from datetime import UTC, datetime

    src = source()
    aid = asunto("Grado", kind="tramite")
    viejo = msg(src, subject="matrícula", occurred=datetime(2026, 1, 27, tzinfo=UTC))
    nuevo = msg(src, subject="informe", occurred=datetime(2026, 9, 4, tzinfo=UTC))
    with connection() as c:
        # se procesa PRIMERO el correo nuevo y DESPUÉS el viejo (backfill): el viejo no pisa
        store.record_state(c, 1, aid, "solo falta inglés", closed=False, inbox_id=nuevo)
        store.record_state(c, 1, aid, "matrícula formalizada", closed=False, inbox_id=viejo)
        a = store.get_asunto(c, 1, aid)
        assert a is not None and a.state_text == "solo falta inglés"
        assert a.state_updated_at is not None and a.state_updated_at.month == 9
        # historial ordenado por fecha efectiva (mensaje), no por orden de asiento
        assert [s.state_text for s in store.states_of(c, 1, aid)] == [
            "matrícula formalizada",
            "solo falta inglés",
        ]
        # sin mensaje (manual) = ahora → gana
        store.record_state(c, 1, aid, "graduado", closed=False, source="manual")
        assert store.get_asunto(c, 1, aid).state_text == "graduado"  # type: ignore[union-attr]
        # rederive repara un vigente pisado
        c.execute(text("UPDATE mod_asuntos SET state_text = 'x' WHERE id = :a"), {"a": aid})
        assert store.rederive_state(c, 1, aid)
        assert store.get_asunto(c, 1, aid).state_text == "graduado"  # type: ignore[union-attr]
        assert not store.rederive_state(c, 1, aid)


def test_merge_mueve_evidencia_estados_relaciones_y_alias(conn: Any) -> None:
    src = source()
    a, b, c3 = asunto("Bootcamp A"), asunto("Bootcamp A (bis)"), asunto("Grado", kind="tramite")
    m1, m2, m3 = msg(src, subject="1"), msg(src, subject="2"), msg(src, subject="3")
    evidence(a, m1)
    evidence(b, m2)
    evidence(b, m1)  # compartida: no debe duplicarse
    evidence(b, m3, hito="Demo day")
    with connection() as c:
        store.add_relation(c, 1, b, c3, "requisito_de", source="llm")
        store.record_state(c, 1, b, "en curso", closed=False)
        assert store.merge_asuntos(c, 1, survivor_id=a, absorbed_id=b)
        sa, sb = store.get_asunto(c, 1, a), store.get_asunto(c, 1, b)
        ev = store.evidence_of(c, 1, a)
        rels = store.relations_of(c, 1, a)
        assert store.resolve_merged(c, 1, b) == a
    assert sa is not None and sb is not None
    assert sb.merged_into_id == a and sb.evidence_count == 0
    assert {e.inbox_id for e in ev} == {m1, m2, m3} and sa.evidence_count == 3
    assert "Bootcamp A (bis)" in sa.aliases and sa.state_text == "en curso"
    assert [(r.relation_type, r.other_id) for r in rels] == [("requisito_de", c3)]
    assert _vertex_exists(a) and not _vertex_exists(b)


def test_merge_traslada_los_datos_clave_sin_duplicar(conn: Any) -> None:
    # Caso real (2026-09-17): el asunto absorbido tenía el enlace del Meet y el superviviente las
    # fuentes manuales; la fusión dejaba los datos del absorbido enterrados en su fila.
    src = source()
    a, b = asunto("Programa de Embajadores de Sideral"), asunto("StarForge Embajadores LATAM")
    m1, m2 = msg(src, subject="1"), msg(src, subject="2")
    with connection() as c:
        datos.add_dato(c, 1, a, label="fuente", value="https://sideral.gitbook.io/ambassador")
        datos.add_dato(c, 1, a, label="", value="lu.ma/StarMakerLATAM", source_inbox_ids=[m1])
        datos.add_dato(c, 1, b, label="Llamada", value="meet.google.com/abc-defg-hij")
        datos.add_dato(
            c, 1, b, label="Calendario", value="LU.MA/starmakerlatam", source_inbox_ids=[m2]
        )
        assert store.merge_asuntos(c, 1, survivor_id=a, absorbed_id=b)
        got = {d["value"]: d for d in datos.datos_of(c, 1, a)}
    assert set(got) == {
        "https://sideral.gitbook.io/ambassador",
        "lu.ma/StarMakerLATAM",
        "meet.google.com/abc-defg-hij",
    }
    assert got["meet.google.com/abc-defg-hij"]["label"] == "Llamada"
    # mismo valor (normalizado): se unen los mensajes de origen y la etiqueta vacía toma la otra
    assert got["lu.ma/StarMakerLATAM"]["inbox_ids"] == sorted([m1, m2])
    assert got["lu.ma/StarMakerLATAM"]["label"] == "Calendario"


def test_relacion_rechaza_inversa_y_autoreferencia(conn: Any) -> None:
    a, b = asunto("Inglés", kind="tramite"), asunto("Grado", kind="tramite")
    with connection() as c:
        assert store.add_relation(c, 1, a, b, "requisito_de")
        assert not store.add_relation(c, 1, b, a, "requisito_de")
        assert not store.add_relation(c, 1, a, a, "parte_de")
        assert not store.add_relation(c, 1, a, b, "requisito_de")  # ya existe


def test_rechazo_de_evidencia_es_definitivo_salvo_alta_explicita(conn: Any) -> None:
    src = source()
    aid = asunto("Hackathon X", kind="hackathon")
    m = msg(src, subject="newsletter")
    with connection() as c:
        assert store.add_evidence(c, 1, aid, m, method="llm") is True
        [ev] = store.evidence_of(c, 1, aid)
        assert store.reject_evidence(c, 1, ev.id) == aid
        # el asignador / la adhesión NO reviven un rechazo
        assert store.add_evidence(c, 1, aid, m, method="llm", hito_text="x") is None
        assert store.add_evidence(c, 1, aid, m, method="hilo") is None
        assert store.is_rejected(c, 1, aid, m)
        assert store.get_asunto(c, 1, aid).evidence_count == 0  # type: ignore[union-attr]
        # una alta explícita (usuario/agente) sí
        assert store.add_evidence(c, 1, aid, m, method="manual", reactivate=True) is False
        assert not store.is_rejected(c, 1, aid, m)
        assert store.get_asunto(c, 1, aid).evidence_count == 1  # type: ignore[union-attr]


def test_revision_de_nombres_e_inactivos_es_incremental(conn: Any) -> None:
    src = source()
    a = asunto("Blockville – Sideral Summer Friday v18.0", kind="evento")  # noqa: RUF001
    b = asunto("solo uno")
    evidence(a, msg(src, subject="1"))
    evidence(a, msg(src, subject="2"))
    evidence(b, msg(src, subject="3"))
    with connection() as c:
        assert [x.id for x in store.unreviewed_names(c, 1)] == [a]  # b tiene 1 evidencia
        store.mark_name_reviewed(c, 1, a)
        assert store.unreviewed_names(c, 1) == []
        store.rename(c, 1, a, "Sideral Summer Fridays (Blockville)")
        assert [x.id for x in store.unreviewed_names(c, 1)] == [a]  # cambió el nombre: vuelve
        store.set_status(c, 1, a, "inactivo")
        assert store.unreviewed_inactive(c, 1) == [a]
        store.mark_inactive_reviewed(c, 1, a)
        assert store.unreviewed_inactive(c, 1) == []


def test_evidencia_dudosa(conn: Any) -> None:
    src = source()
    tram = asunto("Grado", kind="tramite")
    com = asunto("Meetup", kind="comunidad")
    baja = msg(src, subject="algo", from_addr="x@y.com")
    lista = msg(src, subject="Newsletter", from_addr="news@z.com")
    lista2 = msg(src, subject="Newsletter 2", from_addr="news@z.com")
    with connection() as c:
        c.execute(
            text(
                'UPDATE inbox SET payload = payload || \'{"list_id": "l"}\'::jsonb WHERE id = ANY(:i)'  # noqa: E501
            ),
            {"i": [lista, lista2]},
        )
        store.add_evidence(c, 1, tram, baja, method="llm", confidence=0.6)
        store.add_evidence(c, 1, tram, lista, method="llm", confidence=0.9)  # lista en trámite
        store.add_evidence(c, 1, com, lista2, method="llm", confidence=0.9)  # en comunidad: ok
        sus = store.suspicious_evidence(c, 1)
        assert {(s.inbox_id, s.reason.split(" ")[0]) for s in sus} == {
            (baja, "confianza"),
            (lista, "correo"),
        }
        store.mark_evidence_reviewed(c, 1, [sus[0].id])
        assert len(store.suspicious_evidence(c, 1)) == 1


def test_hito_no_se_duplica_por_entrega_doble_ni_por_mismo_texto(conn: Any) -> None:
    from datetime import UTC, datetime

    src = source()
    aid = asunto("Meetup", kind="comunidad")
    t0 = datetime(2026, 5, 4, 17, 7, tzinfo=UTC)
    # la misma invitación de Luma a Gmail y a Outlook: mismo asunto y dominio, Message-ID distinto
    a = msg(
        src,
        subject="You are invited to Meetup v18",
        from_addr="x@user.luma-mail.com",
        occurred=t0,
        message_id="a@ses",
    )
    b = msg(
        src,
        subject="You are invited to Meetup v18",
        from_addr="y@user.luma-mail.com",
        occurred=t0.replace(hour=22),
        message_id="b@ses",
    )
    c3 = msg(src, subject="Otro correo", from_addr="z@otro.com", occurred=t0.replace(day=6))
    with connection() as c:
        assert store.add_evidence(c, 1, aid, a, method="llm", hito_text="Invitación a Meetup v18")
        assert store.add_evidence(
            c, 1, aid, b, method="llm", hito_text="Invitación al Meetup v18 (copia)"
        )
        assert store.add_evidence(c, 1, aid, c3, method="llm", hito_text="Invitación a Meetup v18")
        ev = {e.inbox_id: e for e in store.evidence_of(c, 1, aid)}
    assert ev[a].hito_text == "Invitación a Meetup v18"
    assert ev[b].hito_text is None  # entrega duplicada: evidencia sí, hito no
    assert ev[c3].hito_text is None  # mismo texto normalizado: no se repite
    # la regla del sistema limpia lo asentado antes de la regla
    with connection() as c:
        c.execute(
            text(
                "UPDATE mod_asuntos_evidence SET hito_text = 'Invitación a Meetup v18' "
                "WHERE inbox_id = :i"
            ),
            {"i": b},
        )
        assert store.dedupe_hitos(c, 1) == 1
        assert store.dedupe_hitos(c, 1) == 0


def test_indice_y_revision_de_relaciones(conn: Any) -> None:
    src = source()
    g = asunto("Grado", kind="tramite")
    i = asunto("Inglés", kind="tramite")
    p = asunto("Pagos X", kind="servicio")
    for x in (g, i, p):
        evidence(x, msg(src, subject=f"m{x}a"))
        evidence(x, msg(src, subject=f"m{x}b"))
    with connection() as c:
        assert {a.id for a in store.unreviewed_relations(c, 1)} == {g, i}  # servicio no entra
        assert any(f"A{p} · Pagos X · servicio" in line for line in store.index_lines(c, 1))
        store.mark_relations_reviewed(c, 1, g)
        assert [a.id for a in store.unreviewed_relations(c, 1)] == [i]


def test_forget_borra_candidato_sin_evidencia(conn: Any) -> None:
    src = source()
    aid = asunto("ruido", status="candidato")
    m = msg(src, subject="x")
    evidence(aid, m)
    with connection() as c:
        store.mark_seen(c, 1, [(m, aid, "llm")])
        assert store.unseen_relevant_inbox_ids(c, 1, limit=10) == []
        assert store.forget(c, 1, [m]) == 1
        assert store.get_asunto(c, 1, aid) is None
        assert store.unseen_relevant_inbox_ids(c, 1, limit=10) == [m]


def test_catalog_incluye_cerrados_recientes(conn: Any) -> None:
    a = asunto("abierto")
    b = asunto("cerrado reciente")
    with connection() as c:
        store.record_state(c, 1, b, "fin", closed=True)
        ids = {x.id for x in store.catalog(c, 1, closed_window_days=60)}
        ids0 = {x.id for x in store.catalog(c, 1, closed_window_days=0)}
    assert ids == {a, b}
    assert ids0 == {a, b}  # cerrado hoy entra igual con ventana 0 (>= NOW() - 0 días)


def test_list_busca_por_alias_y_entidades(conn: Any) -> None:
    aid = asunto("Impact Studio")
    with connection() as c:
        store.update_fields(c, 1, aid, entities=["Sideral", "FIB"])
        store.rename(c, 1, aid, "Impact Studio Medellín")
        assert [x.id for x in store.list_asuntos(c, 1, q="sideral")] == [aid]
        assert [x.id for x in store.list_asuntos(c, 1, q="impact studio")] == [aid]
        assert store.list_asuntos(c, 1, q="nada") == []


def test_catalog_around_incluye_cerrados_contemporaneos_al_lote(conn: Any) -> None:
    """Ventana relativa a los MENSAJES: un asunto cerrado hace meses entra si su actividad se
    solapa con las fechas del lote (histórico); no entra para un lote de otra época."""
    from datetime import UTC, datetime

    a = asunto("abierto")
    old = asunto("cerrado viejo")
    with connection() as c:
        c.execute(
            text(
                "UPDATE mod_asuntos SET status='cerrado', first_signal_at=:f, last_signal_at=:l, "
                "closed_at=:cl WHERE id=:i"
            ),
            {
                "f": datetime(2026, 1, 20, tzinfo=UTC),
                "l": datetime(2026, 1, 28, tzinfo=UTC),
                "cl": datetime(2026, 9, 6, tzinfo=UTC),
                "i": old,
            },
        )
        feb = (datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 2, 8, tzinfo=UTC))
        sep = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 8, tzinfo=UTC))
        ids_feb = {x.id for x in store.catalog(c, 1, closed_window_days=60, around=feb)}
        ids_sep = {x.id for x in store.catalog(c, 1, closed_window_days=60, around=sep)}
        ids_now = {x.id for x in store.catalog(c, 1, closed_window_days=60)}
    assert ids_feb == {a, old}  # contemporáneo al lote de febrero (± 60 días)
    assert ids_sep == {a}  # para un lote de septiembre no es contemporáneo
    assert ids_now == {a, old}  # sin `around`: ventana contra hoy (closed_at reciente)


def test_seed_state_usa_hito_o_descripcion_y_no_pisa_estado_real(conn: Any) -> None:
    src = source()
    con_hito = asunto("Pasaporte", kind="tramite", status="abierto")
    sin_hito = asunto("Colpensiones", kind="tramite", status="abierto")
    with connection() as c:
        c.execute(
            text("UPDATE mod_asuntos SET description = 'Afiliación a Colpensiones' WHERE id = :i"),
            {"i": sin_hito},
        )
        from datetime import UTC, datetime

        m = msg(src, subject="Cita", from_addr="a@b.co", occurred=datetime(2026, 2, 3, tzinfo=UTC))
    evidence(con_hito, m, hito="Cita en Cancillería agendada")
    with connection() as c:
        assert store.seed_state(c, 1, con_hito) is True
        assert store.seed_state(c, 1, sin_hito) is True
        assert store.seed_state(c, 1, con_hito) is False  # ya tiene estado: idempotente
        rows = {
            int(r[0]): str(r[1])
            for r in c.execute(
                text("SELECT id, state_text FROM mod_asuntos WHERE id IN (:a, :b)"),
                {"a": con_hito, "b": sin_hito},
            ).all()
        }
        assert rows[con_hito] == "Cita en Cancillería agendada"
        assert rows[sin_hito] == "Afiliación a Colpensiones"
        # una actualización REAL del mismo mensaje pisa la siembra
        store.record_state(c, 1, con_hito, "Pasaporte entregado", closed=False, inbox_id=m)
        assert (
            c.execute(
                text("SELECT state_text FROM mod_asuntos WHERE id = :i"), {"i": con_hito}
            ).scalar()
            == "Pasaporte entregado"
        )
        assert store.seed_missing_states(c, 1) == 0
