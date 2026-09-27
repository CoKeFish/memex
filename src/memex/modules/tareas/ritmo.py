"""RITMO de un asunto: cuánto duran sus consignas y cada cuánto llegan los pasos de sus proyectos,
deducido de sus PROPIOS datos, sin LLM (`determinism-first`).

Por qué (2026-09-24): en un programa de embajadores las dinámicas «flash» duran minutos y la
«dinámica de la semana» se anuncia el martes y cierra el viernes a la hora de la llamada; en un
curso los pasos de un proyecto llegan clase a clase durante semanas. El sistema aplicaba una
constante (7 días) a toda consigna sin plazo y una gracia fija (30 días) a todo proyecto: cosas
abiertas mucho después de vencidas de verdad. Cada asunto tiene su ritmo y se lee de lo que ya pasó:

- **Plazo típico** (`plazo_tipico`): mediana de días entre el anuncio y el plazo, sobre las tareas
  del asunto cuyo plazo vino DICHO en el texto (`due_on` sin `due_source` inferido). Con él se fecha
  una consigna sin plazo (antes: +7 fijos) y se calcula la gracia de una dinámica.
- **Cadencia** (`cadencia_proyecto`): en cuántos días distintos recibió pasos un proyecto y la
  mediana de días entre uno y el siguiente. Un proyecto cuyos pasos llegaron UN solo día es una
  consigna cerrada (una dinámica); uno que los recibe en varios días crece con el curso.
- **Gracia** (`gracia_proyecto`): cuánto puede pasar de la entrega de un proyecto (la mayor fecha de
  sus pasos) antes de darlo por vencido. Consigna de un solo día → el plazo típico del asunto,
  entre 1 y 7 días; pasos en varios días → el doble de su cadencia, entre 7 y 30 días. Sin
  historia no se adivina corto: 30 días, la regla fija anterior.

Todo es determinista e idempotente; un asunto sin datos conserva las reglas fijas anteriores.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from statistics import median

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.localtime import LOCAL_TZ

#: Sin historia, una consigna sin plazo dicho vale una semana (la regla fija anterior).
PLAZO_DEFAULT_DIAS = 7
#: Muestras mínimas para fiarse de la mediana.
MIN_MUESTRAS = 2
#: Un plazo mayor que esto no es una consigna (es otra cosa mal fechada): no cuenta.
PLAZO_MAX_DIAS = 60
#: Gracia de una consigna de un solo día (una dinámica) tras su entrega: [mín, máx] días.
GRACIA_CONSIGNA = (1, 7)
#: Gracia de un proyecto que recibe pasos en varios días (un curso): 2x su cadencia, acotada a
#: [mín, máx]. El máximo es también la gracia de un proyecto SIN historia (la regla fija anterior).
GRACIA_PROYECTO = (7, 30)


def _local_day(ts: object) -> date | None:
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(LOCAL_TZ).date()


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


@dataclass(frozen=True)
class Ritmo:
    #: Mediana de días entre anuncio y plazo dicho; None si no hay muestras suficientes.
    plazo_dias: int | None
    muestras: int

    @property
    def plazo_o_default(self) -> int:
        return self.plazo_dias if self.plazo_dias is not None else PLAZO_DEFAULT_DIAS


def plazo_tipico(conn: Connection, user_id: int, asunto_id: int) -> Ritmo:
    """Cuánto suele durar una consigna en este asunto: mediana de (plazo - día del anuncio) sobre
    las tareas extraídas cuyo plazo vino dicho en el mensaje (sin `due_source` inferido). Un
    mismo anuncio (mismo día y mismo plazo) cuenta una vez aunque haya dejado varias tareas."""
    rows = conn.execute(
        text(
            """
            SELECT t.due_on,
                   (SELECT min(i.occurred_at) FROM inbox i WHERE i.id = ANY(t.source_inbox_ids))
              FROM mod_tareas t
             WHERE t.user_id = :u AND t.asunto_id = :a AND t.origin = 'extraction'
               AND t.due_on IS NOT NULL AND cardinality(t.source_inbox_ids) > 0
               AND t.metadata->>'due_source' IS NULL
               AND (t.metadata->>'is_parent')::boolean IS NOT TRUE
            """
        ),
        {"u": user_id, "a": asunto_id},
    ).all()
    pares: set[tuple[date, date]] = set()
    for due, ts in rows:
        day = _local_day(ts)
        if day is not None and isinstance(due, date):
            pares.add((day, due))
    gaps = [(due - day).days for day, due in pares if 0 <= (due - day).days <= PLAZO_MAX_DIAS]
    if len(gaps) < MIN_MUESTRAS:
        return Ritmo(None, len(gaps))
    return Ritmo(round(median(gaps)), len(gaps))


@dataclass(frozen=True)
class Cadencia:
    #: En cuántos días distintos llegaron pasos al proyecto.
    dias_con_pasos: int
    #: Mediana de días entre un día con pasos y el siguiente; None con menos de dos.
    dias: int | None


def cadencia_proyecto(conn: Connection, user_id: int, project_id: int) -> Cadencia:
    """Cada cuánto recibe pasos un proyecto: los días distintos (hora local) del primer mensaje
    de cada paso no cancelado, y la mediana de días entre ellos."""
    rows = conn.execute(
        text(
            """
            SELECT (SELECT min(i.occurred_at) FROM inbox i WHERE i.id = ANY(c.source_inbox_ids))
              FROM mod_tareas c
             WHERE c.user_id = :u AND c.parent_id = :p AND c.status <> 'cancelada'
            """
        ),
        {"u": user_id, "p": project_id},
    ).all()
    days = sorted({d for (ts,) in rows if (d := _local_day(ts)) is not None})
    if len(days) < 2:
        return Cadencia(len(days), None)
    gaps = [(b - a).days for a, b in pairwise(days)]
    return Cadencia(len(days), round(median(gaps)))


def gracia_proyecto(conn: Connection, user_id: int, project_id: int, asunto_id: int | None) -> int:
    """Días que puede pasar de la entrega de un proyecto antes de darlo por vencido. Pasos de un
    solo día (una dinámica): el plazo típico del asunto, acotado a `GRACIA_CONSIGNA`; si el asunto
    no tiene historia de plazos no se adivina corto: el máximo de `GRACIA_PROYECTO` (30, como
    antes). Pasos en varios días (un proyecto de curso): el doble de su cadencia, acotado a
    `GRACIA_PROYECTO`."""
    cad = cadencia_proyecto(conn, user_id, project_id)
    if cad.dias_con_pasos <= 1:
        ritmo = plazo_tipico(conn, user_id, asunto_id) if asunto_id is not None else Ritmo(None, 0)
        if ritmo.plazo_dias is None:
            return GRACIA_PROYECTO[1]
        return _clamp(ritmo.plazo_dias, *GRACIA_CONSIGNA)
    if cad.dias is None:
        return GRACIA_PROYECTO[1]
    return _clamp(2 * cad.dias, *GRACIA_PROYECTO)
