"""Creación determinista de identidades para REMITENTES DE CHAT desconocidos.

Los chats están allowlisteados (grupos curados): quien escribe ahí es gente real que vale tener en
el directorio aunque el LLM nunca la extraiga como mención. Este paso (lo llama `build_relations`
antes de armar la provenance) garantiza que todo remitente humano de chat exista en
`mod_identidades` con su identificador ESTABLE de plataforma (`kind='platform_id'`, el `user_id`
de Telegram) — la provenance derivada del grafo (brazo CHAT de `vertex_inbox_ids`) resuelve por
ese identificador, así que crear acá = el remitente co-ocurre con lo extraído de sus mensajes.

Decisiones:
- Match SOLO determinista: `platform_id` exacto y, como ENRIQUECIMIENTO, el `handle` de telegram
  ya conocido (si el username era identifier de una identidad, se le ata el `platform_id` en vez
  de crear otra). SIN fuzzy por nombre (doctrina de `resolve.py`: el remitente no se infiere); si
  la misma persona ya existía por email, el pipeline de merge los funde después.
- BOTS: ni crear ni resolver (mismo principio que `is_role_email`: un relay/automatización no
  identifica a una persona única; co-ocurrir con todo lo que postea es ruido). El escape, si algún
  día un bot importa, es quitar el filtro `is_bot`.
- Mensajes de servicio / broadcasts anónimos (`sender` NULL): skip (no hay a quién atar).
- EMAIL y SOCIAL no crean acá: solo RESUELVEN contra identifiers existentes (en email el remitente
  desconocido suele ser ruido que el sistema de calidad ya filtra).

Idempotente: el gating es `NOT EXISTS platform_id` + los `ON CONFLICT DO NOTHING` de
`_insert_identifier`; re-correr no duplica.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.modules.identidades.module import _insert_identifier
from memex.modules.identidades.normalize import norm_identifier

_log = get_logger("memex.modules.identidades.chat_senders")


def _find_unknown_senders(conn: Connection, user_id: int) -> list[dict[str, str | None]]:
    """Remitentes de chat humanos SIN identifier `platform_id` aún: una fila por `user_id` de
    plataforma, con el username/display_name más reciente (el título de la gente cambia)."""
    rows = (
        conn.execute(
            text(
                """
                SELECT DISTINCT ON (i.payload->'sender'->>'user_id')
                       i.payload->'sender'->>'user_id'      AS tg_id,
                       i.payload->'sender'->>'username'     AS username,
                       i.payload->'sender'->>'display_name' AS display_name
                FROM inbox i
                WHERE i.user_id = :u
                  AND i.payload->'sender'->>'user_id' IS NOT NULL
                  AND (i.payload->'sender'->>'is_bot')::boolean IS NOT TRUE
                  AND NOT EXISTS (
                        SELECT 1 FROM mod_identidades_identifiers f
                        WHERE f.user_id = i.user_id AND f.platform = 'telegram'
                          AND f.kind = 'platform_id'
                          AND f.value_norm = i.payload->'sender'->>'user_id')
                ORDER BY i.payload->'sender'->>'user_id', i.id DESC
                """
            ),
            {"u": user_id},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def _identity_by_handle(conn: Connection, user_id: int, username_norm: str) -> int | None:
    """Identidad que ya tiene este username de telegram como identifier `handle`, si la hay."""
    val = conn.execute(
        text(
            """
            SELECT identity_id FROM mod_identidades_identifiers
            WHERE user_id = :u AND platform = 'telegram' AND kind = 'handle'
              AND value_norm = :vn
            ORDER BY identity_id LIMIT 1
            """
        ),
        {"u": user_id, "vn": username_norm},
    ).scalar()
    return int(val) if val is not None else None


def ensure_chat_sender_identities(conn: Connection, user_id: int) -> int:
    """Crea (una sola vez) la identidad `persona` de cada remitente de CHAT aún desconocido y le
    ata su identificador estable (`platform_id`). Si el username ya era identifier de una identidad
    existente, ENRIQUECE (ata el `platform_id` a esa identidad) en vez de crear. Devuelve cuántas
    identidades CREÓ (el enriquecimiento no cuenta)."""
    created = 0
    enriched = 0
    for s in _find_unknown_senders(conn, user_id):
        tg_id = str(s["tg_id"])
        username = s["username"]
        username_norm = norm_identifier("handle", username) if username else ""
        identity_id = _identity_by_handle(conn, user_id, username_norm) if username_norm else None
        if identity_id is None:
            display = (
                s["display_name"]
                or (f"@{username_norm}" if username_norm else "")
                or (f"telegram {tg_id}")
            )
            identity_id = int(
                conn.execute(
                    text(
                        """
                        INSERT INTO mod_identidades
                          (user_id, kind, display_name, source, interest)
                        VALUES (:u, 'persona', :n, 'extraction', FALSE)
                        RETURNING id
                        """
                    ),
                    {"u": user_id, "n": display},
                ).scalar_one()
            )
            created += 1
            if username_norm:
                _insert_identifier(
                    conn, user_id, identity_id, "telegram", "handle", str(username), username_norm
                )
        else:
            enriched += 1
        _insert_identifier(conn, user_id, identity_id, "telegram", "platform_id", tg_id, tg_id)
    if created or enriched:
        _log.info(
            "identidades.chat_senders.done", user_id=user_id, created=created, enriched=enriched
        )
    return created
