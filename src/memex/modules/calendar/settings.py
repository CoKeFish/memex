"""Configuración del módulo calendar en `module_settings.config` (JSONB por-usuario).

Perillas:
- `llm_on_past_events` — ¿el dedup FASE 2 y el merge (los pasos que GASTAN LLM) procesan eventos ya
  vencidos? Default **False** (pedido del dueño: no gastar dinero en eventos que ya pasaron). Lo
  determinista (extracción, consolidación, conflictos, push) NO mira esta perilla: es barata y
  mantiene la historia visible. «Vencido» = la fecha efectiva de fin (`ends_on` o `starts_on`) quedó
  antes de hoy; los pares/grupos salteados quedan tal cual y se retoman si la perilla se prende.
- `asiste_includes_declined` — ¿un invitado que RECHAZÓ la invitación recibe igual la arista
  «asiste» (evento→identidad)? Default **False** (rechazar no es asistir). El tejedor del grafo
  (`relations.deterministic`) la lee con SQL inline; prenderla/apagarla se refleja en la próxima
  consolidación (alta) y en el `reconcile_graph` (baja). El organizador recibe «organiza» siempre.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: Claves en `module_settings.config` del módulo calendar.
LLM_ON_PAST_KEY = "llm_on_past_events"
ASISTE_INCLUDES_DECLINED_KEY = "asiste_includes_declined"


def _get_bool(conn: Connection, user_id: int, key: str) -> bool:
    """Lee una perilla booleana de `module_settings.config` del módulo calendar. Ausente ⇒ False."""
    val = conn.execute(
        text(
            "SELECT config->>:k FROM module_settings "
            "WHERE user_id = :uid AND module_slug = 'calendar'"
        ),
        {"k": key, "uid": user_id},
    ).scalar()
    return val == "true"


def _set_bool(conn: Connection, user_id: int, key: str, value: bool) -> None:
    """Upsert de una perilla booleana en `module_settings.config`; merge (`||`) preserva las demás
    claves y no toca `enabled`."""
    conn.execute(
        text(
            """
            INSERT INTO module_settings (user_id, module_slug, config)
            VALUES (:uid, 'calendar',
                    jsonb_build_object(CAST(:k AS text), CAST(:v AS boolean)))
            ON CONFLICT (user_id, module_slug) DO UPDATE
              SET config = module_settings.config || EXCLUDED.config
            """
        ),
        {"uid": user_id, "k": key, "v": value},
    )


def llm_on_past_events(conn: Connection, user_id: int) -> bool:
    """¿Gastar LLM en eventos pasados? Ausente ⇒ False (no gastar)."""
    return _get_bool(conn, user_id, LLM_ON_PAST_KEY)


def set_llm_on_past_events(conn: Connection, user_id: int, value: bool) -> None:
    """Upsert de la perilla en `module_settings.config`; no toca `enabled` ni otras claves."""
    _set_bool(conn, user_id, LLM_ON_PAST_KEY, value)


def asiste_includes_declined(conn: Connection, user_id: int) -> bool:
    """¿Un invitado `declined` recibe la arista «asiste»? Ausente ⇒ False (rechazar no es asistir).
    La lee el tejedor del grafo con SQL inline (ver `relations.deterministic`); este reader/writer
    es para la superficie de control (CLI/API/UI)."""
    return _get_bool(conn, user_id, ASISTE_INCLUDES_DECLINED_KEY)


def set_asiste_includes_declined(conn: Connection, user_id: int, value: bool) -> None:
    """Upsert de la perilla `asiste_includes_declined`; merge preserva `llm_on_past_events`."""
    _set_bool(conn, user_id, ASISTE_INCLUDES_DECLINED_KEY, value)
