"""Configuración del módulo calendar en `module_settings.config` (JSONB por-usuario).

Hoy una sola perilla: `llm_on_past_events` — ¿el dedup FASE 2 y el merge (los pasos que GASTAN
LLM) procesan eventos ya vencidos? Default **False** (pedido del dueño: no gastar dinero en
eventos que ya pasaron). Lo determinista (extracción, consolidación, conflictos, push) NO mira
esta perilla: es barata y mantiene la historia visible. «Vencido» = la fecha efectiva de fin
(`ends_on` o `starts_on`) quedó antes de hoy; los pares/grupos salteados quedan tal cual y se
retoman si la perilla se prende.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

#: Clave en `module_settings.config` del módulo calendar.
LLM_ON_PAST_KEY = "llm_on_past_events"


def llm_on_past_events(conn: Connection, user_id: int) -> bool:
    """¿Gastar LLM en eventos pasados? Ausente ⇒ False (no gastar)."""
    val = conn.execute(
        text(
            "SELECT config->>:k FROM module_settings "
            "WHERE user_id = :uid AND module_slug = 'calendar'"
        ),
        {"k": LLM_ON_PAST_KEY, "uid": user_id},
    ).scalar()
    return val == "true"


def set_llm_on_past_events(conn: Connection, user_id: int, value: bool) -> None:
    """Upsert de la perilla en `module_settings.config`; no toca `enabled` ni otras claves."""
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
        {"uid": user_id, "k": LLM_ON_PAST_KEY, "v": value},
    )
