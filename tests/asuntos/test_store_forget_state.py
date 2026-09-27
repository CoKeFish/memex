"""Reproceso (`store.forget`): olvidar unos mensajes también olvida los ESTADOS que produjeron, y el
vigente vuelve a ser el más reciente de los que quedan (por fecha del mensaje). Sin esto, al
re-leer una charla con otra regla («ánimo no es estado») el asunto seguía diciendo lo que aquella
lectura dejó."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memex.db import connection
from memex.modules.asuntos import store
from tests.asuntos._seed import asunto, evidence, msg, source

_T1 = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
_T2 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def test_forget_borra_los_estados_de_esos_mensajes_y_restaura_el_anterior(conn: Any) -> None:
    src = source()
    aid = asunto("Postulación X")
    m1 = msg(src, subject="a", occurred=_T1)
    m2 = msg(src, subject="b", occurred=_T2)
    evidence(aid, m1)
    evidence(aid, m2)
    with connection() as c:
        store.record_state(c, 1, aid, "Va a grabar el video", closed=False, inbox_id=m1)
        store.record_state(c, 1, aid, "No quiere hacer el video", closed=False, inbox_id=m2)
        assert store.get_asunto(c, 1, aid).state_text == "No quiere hacer el video"  # type: ignore[union-attr]

    with connection() as c:
        store.forget(c, 1, [m2])
        a = store.get_asunto(c, 1, aid)
    assert a is not None
    assert a.state_text == "Va a grabar el video"
    assert a.state_updated_at == _T1

    with connection() as c:
        store.forget(c, 1, [m1])
        a = store.get_asunto(c, 1, aid)
    assert a is not None and a.state_text == "" and a.state_updated_at is None


def test_restore_state_prefiere_la_fecha_del_mensaje_al_orden_de_asiento(conn: Any) -> None:
    # Un backfill asentó DESPUÉS un estado de un mensaje MÁS VIEJO: al restaurar, gana el estado
    # del mensaje más reciente, no el último asentado.
    src = source()
    aid = asunto("Trámite Y")
    viejo = msg(src, subject="v", occurred=_T1)
    nuevo = msg(src, subject="n", occurred=_T2)
    with connection() as c:
        store.record_state(c, 1, aid, "estado nuevo", closed=False, inbox_id=nuevo)
        store.record_state(c, 1, aid, "estado viejo", closed=False, inbox_id=viejo)
        assert store.restore_state(c, 1, aid) == "estado nuevo"
