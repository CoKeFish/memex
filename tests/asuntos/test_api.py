from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.asuntos import store
from tests.asuntos._seed import asunto, evidence, msg, source


def test_list_story_y_acciones(client: Any) -> None:
    src = source()
    a = asunto("Grado Salesiana 2026", kind="tramite")
    ing = asunto("Examen de inglés", kind="tramite")
    cand = asunto("candidato", status="candidato")
    m1 = msg(src, subject="Requisitos de grado", from_addr="carrera@salesiana.edu.co")
    m2 = msg(src, subject="Informe de avance", from_addr="carrera@salesiana.edu.co")
    evidence(a, m1, hito="La carrera revisó el historial")
    evidence(a, m2)
    with connection() as c:
        store.record_state(c, 1, a, "solo falta inglés", closed=False)
        store.add_relation(c, 1, ing, a, "requisito_de")

    r = client.get("/asuntos")
    assert r.status_code == 200
    body = r.json()
    assert body["counts"] == {"abierto": 2, "candidato": 1}
    assert {x["id"] for x in body["items"]} == {a, ing, cand}
    assert client.get("/asuntos?status=candidato").json()["items"][0]["id"] == cand
    assert [x["id"] for x in client.get("/asuntos?q=examen").json()["items"]] == [ing]

    s = client.get(f"/asuntos/{a}").json()
    assert s["asunto"]["state_text"] == "solo falta inglés"
    assert [h["text"] for h in s["hitos"]] == ["La carrera revisó el historial"]
    assert {e["inbox_id"] for e in s["evidence"]} == {m1, m2}
    assert s["evidence"][0]["kind"] == "email"
    assert s["relations"][0]["relation_type"] == "requisito_de"
    assert s["relations"][0]["direction"] == "in" and s["relations"][0]["other_id"] == ing
    assert client.get("/asuntos/999999").status_code == 404

    r = client.post(f"/asuntos/{a}/rename", json={"name": "Grado PUJ", "kind": "tramite"})
    assert r.status_code == 200 and "Grado Salesiana 2026" in r.json()["aliases"]
    r = client.post(f"/asuntos/{a}/close", json={"state_text": "graduado"})
    assert r.json()["status"] == "cerrado" and r.json()["state_text"] == "graduado"
    r = client.post(f"/asuntos/{a}/reopen", json={"state_text": ""})
    assert r.json()["status"] == "abierto"
    ev_id = s["evidence"][1]["id"]
    assert client.post(f"/asuntos/evidence/{ev_id}/reject").json() == {"asunto_id": a}
    assert client.get(f"/asuntos/{a}").json()["asunto"]["evidence_count"] == 1
    r = client.post(
        "/asuntos/relate", json={"src_id": cand, "dst_id": a, "relation_type": "parte_de"}
    )
    assert r.json() == {"created": True}
    r = client.post("/asuntos/merge", json={"survivor_id": a, "absorbed_id": ing})
    assert r.status_code == 200 and "Examen de inglés" in r.json()["aliases"]
    assert client.delete(f"/asuntos/{cand}").json() == {"deleted": True}
    assert client.delete(f"/asuntos/{cand}").status_code == 404


def test_settings_get_patch(client: Any) -> None:
    assert client.get("/asuntos/settings").json()["enabled"] is True
    r = client.patch("/asuntos/settings", json={"batch_size": 20, "adhesion_enabled": False})
    assert r.status_code == 200 and r.json()["batch_size"] == 20
    assert r.json()["adhesion_enabled"] is False and r.json()["enabled"] is True
    assert client.patch("/asuntos/settings", json={"batch_size": 0}).status_code == 422


def test_list_trae_que_sigue_de_tareas(client: Any) -> None:
    """La lista (y la story) traen cuántas tareas pendientes vivas tiene el asunto y la fecha más
    próxima: los proyectos (contenedores) y las tareas cerradas no cuentan."""
    from datetime import date

    from memex.modules.tareas import module as tareas

    a = asunto("Curso con tareas")
    with connection() as c:
        tareas.add(c, 1, title="Entregar ensayo", due_on=date(2030, 3, 5), asunto=a)
        tareas.add(c, 1, title="Leer capítulo", due_on=date(2030, 2, 1), asunto=a)
        tareas.add(c, 1, title="Sin fecha", asunto=a)
        proy = tareas.add(c, 1, title="Proyecto final", asunto=a, metadata={"is_parent": True})
        # Un paso viejo no adelanta la fecha: el proyecto cuenta por su ENTREGA (la mayor).
        paso_a = tareas.add(c, 1, title="Paso A", due_on=date(2029, 1, 1), asunto=a)
        paso_b = tareas.add(c, 1, title="Paso B", due_on=date(2030, 6, 1), asunto=a)
        tareas.update(c, 1, int(paso_a["id"]), parent=int(proy["id"]))
        tareas.update(c, 1, int(paso_b["id"]), parent=int(proy["id"]))
        hecha = tareas.add(c, 1, title="Ya hecha", due_on=date(2030, 1, 1), asunto=a)
        tareas.set_status(c, 1, int(hecha["id"]), "completada")
    row = next(x for x in client.get("/asuntos").json()["items"] if x["id"] == a)
    assert row["tareas_pendientes"] == 5  # 3 sueltas + 2 pasos; el proyecto no cuenta
    assert row["proxima_fecha"] == "2030-02-01"
    story = client.get(f"/asuntos/{a}").json()["asunto"]
    assert story["tareas_pendientes"] == 5 and story["proxima_fecha"] == "2030-02-01"
    # Solo el proyecto con pasos: la fecha es su entrega (2030-06-01), no el paso de 2029.
    solo = asunto("Solo proyecto")
    with connection() as c:
        p2 = tareas.add(c, 1, title="Proyecto", asunto=solo, metadata={"is_parent": True})
        s1 = tareas.add(c, 1, title="Paso 1", due_on=date(2029, 1, 1), asunto=solo)
        s2 = tareas.add(c, 1, title="Paso 2", due_on=date(2030, 6, 1), asunto=solo)
        tareas.update(c, 1, int(s1["id"]), parent=int(p2["id"]))
        tareas.update(c, 1, int(s2["id"]), parent=int(p2["id"]))
    row = next(x for x in client.get("/asuntos").json()["items"] if x["id"] == solo)
    assert row["tareas_pendientes"] == 2 and row["proxima_fecha"] == "2030-06-01"
    vacio = asunto("Sin tareas")
    row = next(x for x in client.get("/asuntos").json()["items"] if x["id"] == vacio)
    assert row["tareas_pendientes"] == 0 and row["proxima_fecha"] is None


def test_story_documento_trae_nombre_y_descarga(client: Any) -> None:
    """Un archivo adjunto al asunto se ve con su nombre real y con el id del blob para bajarlo."""
    from sqlalchemy import text

    a = asunto("Curso con archivos")
    with connection() as c:
        src = int(
            c.execute(
                text(
                    "INSERT INTO sources (user_id, name, type) VALUES (1, 'documentos-test', "
                    "'documentos') RETURNING id"
                )
            ).scalar_one()
        )
        iid = int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (1, :s, 'sha256:doc1', NOW(), CAST(:p AS JSONB)) RETURNING id"
                ),
                {
                    "s": src,
                    "p": '{"filename": "B1-1_3.pdf", "content_type": "application/pdf", '
                    '"size": 1234, "captured_from": "web"}',
                },
            ).scalar_one()
        )
        mid = int(
            c.execute(
                text(
                    "INSERT INTO media_assets (user_id, inbox_id, sha256, object_key, bucket, "
                    "content_type, size_bytes, filename) VALUES (1, :i, 'abc', 'media/1/abc', "
                    "'memex', 'application/pdf', 1234, 'B1-1_3.pdf') RETURNING id"
                ),
                {"i": iid},
            ).scalar_one()
        )
        store.add_evidence(c, 1, a, iid, method="manual")
    ev = client.get(f"/asuntos/{a}").json()["evidence"]
    assert len(ev) == 1
    assert ev[0]["subject"] == "B1-1_3.pdf" and ev[0]["filename"] == "B1-1_3.pdf"
    assert ev[0]["media_id"] == mid and ev[0]["size_bytes"] == 1234


def test_story_chat_evidence_shows_a_readable_sender(client: Any) -> None:
    """Un mensaje de chat trae `sender` como objeto: se ve el nombre, «yo» si es propio o el
    teléfono; nunca el JSON crudo."""
    import json

    from sqlalchemy import text

    a = asunto("Curso con chat")
    with connection() as c:
        src = int(
            c.execute(
                text(
                    "INSERT INTO sources (user_id, name, type) VALUES (1, 'wa-test', 'whatsapp') "
                    "RETURNING id"
                )
            ).scalar_one()
        )
        ids = []
        for sender in (
            {"phone": "573001", "display_name": "Ana", "is_self": False},
            {"phone": None, "display_name": None, "is_self": True},
            {"phone": "573002", "display_name": None, "is_self": False},
        ):
            ids.append(
                int(
                    c.execute(
                        text(
                            "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, "
                            "payload) VALUES (1, :s, :e, NOW(), CAST(:p AS JSONB)) RETURNING id"
                        ),
                        {
                            "s": src,
                            "e": f"wa:{sender.get('phone')}:{sender['is_self']}",
                            "p": json.dumps(
                                {"chat_id": "g@g.us", "text": "hola", "sender": sender}
                            ),
                        },
                    ).scalar_one()
                )
            )
        for iid in ids:
            store.add_evidence(c, 1, a, iid, method="manual")
    by_inbox = {e["inbox_id"]: e["sender"] for e in client.get(f"/asuntos/{a}").json()["evidence"]}
    assert [by_inbox[i] for i in ids] == ["Ana", "yo", "573002"]


def test_silenciar_y_dejar_de_silenciar_por_la_api(client: Any) -> None:
    """El botón que usa el dueño: silenciar cierra, cancela pendientes y deja el asunto quieto."""
    a = asunto("TerraMaster TNAS F2-425", kind="servicio")
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_tareas (user_id, title, description, status, asunto_id) "
                "VALUES (1, 'Actualizar el certificado', '', 'pendiente', :a)"
            ),
            {"a": a},
        )
    r = client.post(f"/asuntos/{a}/silence")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cerrado" and body["silenced_at"] is not None
    assert body["tareas_pendientes"] == 0

    # el curador no lo revive
    with connection() as c:
        assert store.set_status(c, 1, a, "abierto", source="curator") is False

    r = client.post(f"/asuntos/{a}/unsilence")
    assert r.status_code == 200 and r.json()["silenced_at"] is None
    assert r.json()["status"] == "cerrado", "levantar el silencio no reabre por sí solo"
    assert client.post("/asuntos/999999/silence").status_code == 404


def _tarea_en(asunto_id: int, title: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_tareas (user_id, title, description, status, asunto_id) "
                    "VALUES (1, :t, '', 'pendiente', :a) RETURNING id"
                ),
                {"t": title, "a": asunto_id},
            ).scalar_one()
        )


def _estado(tarea_id: int) -> str:
    with connection() as c:
        return str(
            c.execute(
                text("SELECT status FROM mod_tareas WHERE id = :i"), {"i": tarea_id}
            ).scalar_one()
        )


def test_cerrar_deja_las_pendientes_como_estaban_por_default(client: Any) -> None:
    """El comportamiento de siempre: cerrar no tocaba las tareas."""
    a = asunto("Hackathon que paso", kind="hackathon")
    t = _tarea_en(a, "Inscribirse al hackathon")
    assert client.post(f"/asuntos/{a}/close", json={"state_text": "paso"}).status_code == 200
    assert _estado(t) == "pendiente"


def test_cerrar_puede_dar_por_hechas_las_pendientes(client: Any) -> None:
    """El tramite que terminaste bien: lo que quedaba pendiente ya lo hiciste."""
    a = asunto("Tramite terminado", kind="tramite")
    t = _tarea_en(a, "Reclamar el documento")
    r = client.post(f"/asuntos/{a}/close", json={"state_text": "listo", "tareas": "completar"})
    assert r.status_code == 200 and r.json()["status"] == "cerrado"
    assert _estado(t) == "completada"


def test_cerrar_puede_cancelar_lo_que_ya_no_aplica(client: Any) -> None:
    """El evento que se te paso: la inscripcion ya no se puede hacer."""
    a = asunto("Evento vencido", kind="evento")
    t = _tarea_en(a, "Inscribirse")
    r = client.post(f"/asuntos/{a}/close", json={"state_text": "paso", "tareas": "cancelar"})
    assert r.status_code == 200
    assert _estado(t) == "cancelada"


def test_una_accion_invalida_se_rechaza(client: Any) -> None:
    a = asunto("X", kind="tramite")
    r = client.post(f"/asuntos/{a}/close", json={"state_text": "", "tareas": "borrar"})
    assert r.status_code == 422
