"""Settings de proveedor LLM por consumidor (`llm_consumer_settings`).

Una fila por (user_id, consumer): qué proveedor + modelo usa cada proceso que consume LLM cuando
el caller no inyecta un cliente. Lo lee la fábrica `memex.llm.registry.build_llm_client`. Patrón
`relevance/settings.py`: la DB manda en runtime; SIN fila para el consumer se resuelve la fila
`default`; sin esa, el hardcode DeepSeek (preserva el comportamiento previo a esta tabla).

`fallback` es la lista ORDENADA de proveedores extra que prueba el `FallbackClient` si el primario
agota cuota/red-5xx/timeout (cadena efectiva = [provider, *fallback]).

`deadline_s` sube o baja el tope de reloj de pared por llamada (`LLMConfig.deadline_s`, 180 s
por default) para los consumidores que tardan de verdad —un modelo de razonamiento puede pasarse
de los 5 minutos y no es un cuelgue—. `None` = el default del cliente.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, text

#: Proveedores válidos (claves de la fábrica). Mismo set que el CHECK (0068 + openai en 0079).
LLM_PROVIDERS = ("deepseek", "anthropic", "codex", "openai")

#: Fila comodín cuando no hay una específica para el consumer.
DEFAULT_CONSUMER = "default"

#: Proveedor de último recurso (sin fila ni default): el que todos los workers usaban hardcodeado.
_HARDCODE_PROVIDER = "deepseek"

#: Claves de consumer válidas (una por punto de construcción de la fábrica) + `default`. Mantener
#: en sync con las llamadas a `build_llm_client` en los workers.
LLM_CONSUMERS = (
    DEFAULT_CONSUMER,
    "summarizer",
    "orchestrator",
    "process",
    "calendar_dedup",
    "calendar_merge",
    "finance_dedup",
    "identidades_dedup",
    "identidades_cooccurrence",
    "identidades_hierarchy",
    "identidades_classify",
    "identidades_resolve",
    "relations_confirm",
    "relations_clusters",
    "asuntos_assign",
    # Segunda verificación de fechas de calendar (CAP_VERIFY): arbitraje → codex (regla del
    # dueño 2026-09-07: DeepSeek para el volumen, codex para juzgar/confirmar/verificar).
    "calendar_date_verify",
    # Extracción del módulo TAREAS en su propia unidad (InterestModule.llm_consumer): el dueño
    # pidió que esta parte funcione bien aunque haya que pagar con codex (2026-09-13). Default
    # DeepSeek como el resto; `memex-llm settings set --consumer extract_tareas --provider codex
    # --fallback deepseek` lo manda a codex.
    "extract_tareas",
    # Juez de tareas: «¿la candidata es la misma tarea que la persona ya completó?». Arbitraje →
    # codex (settings set --consumer tareas_judge --provider codex --fallback deepseek).
    "tareas_judge",
    # Juez de asuntos gemelos: «¿el asunto que el asignador quiere abrir ya es una cosa del
    # catálogo (otro nombre, otro canal)?». Arbitraje → codex (settings set --consumer
    # asuntos_judge --provider codex --fallback deepseek).
    "asuntos_judge",
    # Cerebro del asistente de Telegram (chat con herramientas + streaming): DeepSeek por
    # default; solo proveedores OpenAI-compatible (deepseek | openai). Codex es el fallback.
    "assistant_brain",
    # Redactor de avisos del daemon de notificaciones (resumen diario con render=llm):
    # DeepSeek por default; barato y sin arbitraje.
    "notifications",
    # Intérprete de «contar algo» en un asunto: lee la nota del dueño con los pasos
    # pendientes y devuelve qué cambia (fecha a un paso, paso hecho, plazo, estado).
    # Barato y acotado: DeepSeek por default.
    "asuntos_nota",
    # Reclasificación de rubros de finanzas («otros» → lo que es), por lotes de
    # contrapartes: barato, DeepSeek por default.
    "finance_categorize",
)


@dataclass(frozen=True)
class LLMConsumerSettings:
    """Proveedor+modelo resueltos para un consumer.

    `provider` ∈ `LLM_PROVIDERS`. `model` None = el default del proveedor (el `default_model` del
    cliente; codex lo ignora). `codex_model` solo aplica cuando el proveedor (primario o de
    fallback) es codex. `fallback` = cadena ordenada de proveedores extra.
    """

    provider: str = _HARDCODE_PROVIDER
    model: str | None = None
    codex_model: str | None = None
    fallback: tuple[str, ...] = field(default_factory=tuple)
    #: Tope de reloj de pared por llamada, en segundos; None = el default del cliente.
    deadline_s: float | None = None


def _coerce_fallback(raw: Any) -> tuple[str, ...]:
    """`fallback` viene de JSONB (psycopg → list) o, defensivamente, como str JSON."""
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    if not isinstance(raw, list):
        return ()
    return tuple(str(p) for p in raw)


def _row_to_settings(row: dict[str, Any]) -> LLMConsumerSettings:
    return LLMConsumerSettings(
        provider=str(row["provider"]),
        model=str(row["model"]) if row["model"] is not None else None,
        codex_model=str(row["codex_model"]) if row["codex_model"] is not None else None,
        fallback=_coerce_fallback(row["fallback"]),
        deadline_s=float(row["deadline_s"]) if row.get("deadline_s") is not None else None,
    )


def get_consumer_settings(conn: Connection, user_id: int, consumer: str) -> LLMConsumerSettings:
    """Settings resueltos del consumer: fila propia → fila `default` → hardcode DeepSeek.

    Una sola query elige la fila exacta del consumer si existe, si no la `default` (ORDER BY el
    match exacto primero). Sin ninguna de las dos → `LLMConsumerSettings()` (DeepSeek, como antes).
    """
    row = (
        conn.execute(
            text(
                "SELECT provider, model, codex_model, fallback, deadline_s "
                "FROM llm_consumer_settings "
                "WHERE user_id = :uid AND consumer IN (:consumer, :default) "
                "ORDER BY (consumer = :consumer) DESC LIMIT 1"
            ),
            {"uid": user_id, "consumer": consumer, "default": DEFAULT_CONSUMER},
        )
        .mappings()
        .first()
    )
    if row is None:
        return LLMConsumerSettings()
    return _row_to_settings(dict(row))


def _get_exact(conn: Connection, user_id: int, consumer: str) -> LLMConsumerSettings | None:
    """La fila EXACTA del consumer (sin caer al default) — base del upsert parcial."""
    row = (
        conn.execute(
            text(
                "SELECT provider, model, codex_model, fallback, deadline_s "
                "FROM llm_consumer_settings "
                "WHERE user_id = :uid AND consumer = :consumer"
            ),
            {"uid": user_id, "consumer": consumer},
        )
        .mappings()
        .first()
    )
    return _row_to_settings(dict(row)) if row is not None else None


def list_consumer_settings(conn: Connection, user_id: int) -> dict[str, LLMConsumerSettings]:
    """Todas las filas del usuario, por consumer (para `show` del CLI y el endpoint)."""
    rows = conn.execute(
        text(
            "SELECT consumer, provider, model, codex_model, fallback, deadline_s "
            "FROM llm_consumer_settings "
            "WHERE user_id = :uid ORDER BY consumer"
        ),
        {"uid": user_id},
    ).mappings()
    return {str(r["consumer"]): _row_to_settings(dict(r)) for r in rows}


def upsert_consumer_settings(
    conn: Connection,
    user_id: int,
    consumer: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    codex_model: str | None = None,
    fallback: Sequence[str] | None = None,
    deadline_s: float | None = None,
) -> LLMConsumerSettings:
    """Upsert PARCIAL (solo los campos pasados) de la fila del consumer; devuelve el resultado.

    `consumer`/`provider`/`fallback` inválidos → ValueError (el CHECK de la DB también rechazaría
    el provider, pero el error de capa de aplicación es accionable para API/CLI). `model=""` y
    `codex_model=""` LIMPIAN el override (vuelven al default del proveedor); `deadline_s=0`
    limpia el tope propio (vuelve al default del cliente).
    """
    if consumer not in LLM_CONSUMERS:
        raise ValueError(f"consumer inválido: {consumer!r}; válidos: {LLM_CONSUMERS}")
    if provider is not None and provider not in LLM_PROVIDERS:
        raise ValueError(f"provider inválido: {provider!r}; válidos: {LLM_PROVIDERS}")
    if fallback is not None:
        bad = [p for p in fallback if p not in LLM_PROVIDERS]
        if bad:
            raise ValueError(f"fallback inválido: {bad}; válidos: {LLM_PROVIDERS}")
    if deadline_s is not None and deadline_s < 0:
        raise ValueError(f"deadline_s inválido: {deadline_s!r} (segundos, >= 0)")

    current = _get_exact(conn, user_id, consumer) or LLMConsumerSettings()
    resolved_model = current.model if model is None else (model.strip() or None)
    resolved_codex = current.codex_model if codex_model is None else (codex_model.strip() or None)
    resolved = LLMConsumerSettings(
        provider=current.provider if provider is None else provider,
        model=resolved_model,
        codex_model=resolved_codex,
        fallback=current.fallback if fallback is None else tuple(fallback),
        deadline_s=(current.deadline_s if deadline_s is None else (deadline_s or None)),
    )
    conn.execute(
        text(
            """
            INSERT INTO llm_consumer_settings (user_id, consumer, provider, model, codex_model,
                                               fallback, deadline_s)
            VALUES (:uid, :consumer, :provider, :model, :codex_model, CAST(:fallback AS JSONB),
                    :deadline_s)
            ON CONFLICT (user_id, consumer) DO UPDATE
                SET provider = EXCLUDED.provider, model = EXCLUDED.model,
                    codex_model = EXCLUDED.codex_model, fallback = EXCLUDED.fallback,
                    deadline_s = EXCLUDED.deadline_s, updated_at = NOW()
            """
        ),
        {
            "uid": user_id,
            "consumer": consumer,
            "provider": resolved.provider,
            "model": resolved.model,
            "codex_model": resolved.codex_model,
            "fallback": json.dumps(list(resolved.fallback)),
            "deadline_s": resolved.deadline_s,
        },
    )
    return resolved
