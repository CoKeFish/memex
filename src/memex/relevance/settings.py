"""Settings del gate de relevancia (`relevance_gate_settings`) — una fila por usuario.

Tabla PROPIA y no `module_settings`: el gate no es un InterestModule (un slug ahí rompería
`resolve()` del registry y `PATCH /modules/{slug}`). Patrón `scheduler_settings`: la DB manda
en runtime, sin fila → defaults APAGADOS (procesamiento apagado por default).

`mode` es la perilla del experimento del dueño: `per_window` (1 llamada LLM por ventana con
veredictos por mensaje) vs `per_message` (1 llamada por correo).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text

GATE_MODES = ("per_window", "per_message")
GATE_PROVIDERS = ("anthropic", "codex", "deepseek")
_DEFAULT_MODEL = "claude-opus-4-8"


@dataclass(frozen=True)
class GateSettings:
    """Settings resueltos del gate para un usuario.

    `mining_min_messages`: umbral de acumulación de la minería — solo se proponen reglas para
    clases (remitentes) con N+ correos no-relevantes; un solo correo malo nunca dispara nada.
    `provider`: quién juzga — 'anthropic' (API, default) o 'codex' (`codex exec` con la
    suscripción del dueño; SOLO host-side, sin métricas de tokens). `model` pertenece al path
    Anthropic; `codex_model` al de codex (None = el default del CLI).

    `mining_interleave`: minar reglas ENTRE lotes (procesamiento incremental) — cada lote
    aprovecha las reglas que destiló el anterior. `interest_suggest_min_marks`: umbral del
    segundo lazo (rechazos manuales acumulados antes de sugerir editar intereses).
    """

    enabled: bool = False
    mode: str = "per_window"
    model: str = _DEFAULT_MODEL
    mining_min_messages: int = 3
    provider: str = "anthropic"
    codex_model: str | None = None
    mining_interleave: bool = True
    interest_suggest_min_marks: int = 5

    @property
    def complete_model(self) -> str | None:
        """Modelo a pasar a `complete()`: `model` pertenece a Anthropic. Codex lo IGNORA (usa su
        `codex_model`) y DeepSeek usaría un nombre inválido (`claude-opus-*`) → None = el default
        del cliente. Es lo que hace al proveedor intercambiable sin tocar `model`."""
        return self.model if self.provider == "anthropic" else None


def get_settings(conn: Connection, user_id: int) -> GateSettings:
    """Settings del gate del usuario; sin fila → defaults apagados."""
    row = (
        conn.execute(
            text(
                "SELECT enabled, mode, model, mining_min_messages, provider, codex_model, "
                "mining_interleave, interest_suggest_min_marks "
                "FROM relevance_gate_settings WHERE user_id = :uid"
            ),
            {"uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return GateSettings()
    return GateSettings(
        enabled=bool(row["enabled"]),
        mode=str(row["mode"]),
        model=str(row["model"]),
        mining_min_messages=int(row["mining_min_messages"]),
        provider=str(row["provider"]),
        codex_model=str(row["codex_model"]) if row["codex_model"] is not None else None,
        mining_interleave=bool(row["mining_interleave"]),
        interest_suggest_min_marks=int(row["interest_suggest_min_marks"]),
    )


def upsert_settings(
    conn: Connection,
    user_id: int,
    *,
    enabled: bool | None = None,
    mode: str | None = None,
    model: str | None = None,
    mining_min_messages: int | None = None,
    provider: str | None = None,
    codex_model: str | None = None,
    mining_interleave: bool | None = None,
    interest_suggest_min_marks: int | None = None,
) -> GateSettings:
    """Upsert PARCIAL (solo los campos pasados); devuelve los settings resultantes.

    `mode`/`mining_min_messages`/`provider`/`interest_suggest_min_marks` inválidos → ValueError
    (el CHECK de la DB también los rechazaría, pero el error de capa de aplicación es accionable
    para API/CLI). `codex_model=""` limpia el override (vuelve al default del CLI de codex).
    """
    if mode is not None and mode not in GATE_MODES:
        raise ValueError(f"mode inválido: {mode!r}; válidos: {GATE_MODES}")
    if mining_min_messages is not None and mining_min_messages < 1:
        raise ValueError(f"mining_min_messages inválido: {mining_min_messages} (mínimo 1)")
    if provider is not None and provider not in GATE_PROVIDERS:
        raise ValueError(f"provider inválido: {provider!r}; válidos: {GATE_PROVIDERS}")
    if interest_suggest_min_marks is not None and interest_suggest_min_marks < 1:
        raise ValueError(
            f"interest_suggest_min_marks inválido: {interest_suggest_min_marks} (mínimo 1)"
        )
    current = get_settings(conn, user_id)
    resolved_codex_model = current.codex_model
    if codex_model is not None:
        resolved_codex_model = codex_model.strip() or None
    resolved = GateSettings(
        enabled=current.enabled if enabled is None else enabled,
        mode=current.mode if mode is None else mode,
        model=current.model if model is None else model,
        mining_min_messages=(
            current.mining_min_messages if mining_min_messages is None else mining_min_messages
        ),
        provider=current.provider if provider is None else provider,
        codex_model=resolved_codex_model,
        mining_interleave=(
            current.mining_interleave if mining_interleave is None else mining_interleave
        ),
        interest_suggest_min_marks=(
            current.interest_suggest_min_marks
            if interest_suggest_min_marks is None
            else interest_suggest_min_marks
        ),
    )
    conn.execute(
        text(
            """
            INSERT INTO relevance_gate_settings (user_id, enabled, mode, model,
                                                 mining_min_messages, provider, codex_model,
                                                 mining_interleave, interest_suggest_min_marks)
            VALUES (:uid, :enabled, :mode, :model, :mining_min, :provider, :codex_model,
                    :mining_interleave, :interest_min)
            ON CONFLICT (user_id) DO UPDATE
                SET enabled = EXCLUDED.enabled, mode = EXCLUDED.mode, model = EXCLUDED.model,
                    mining_min_messages = EXCLUDED.mining_min_messages,
                    provider = EXCLUDED.provider, codex_model = EXCLUDED.codex_model,
                    mining_interleave = EXCLUDED.mining_interleave,
                    interest_suggest_min_marks = EXCLUDED.interest_suggest_min_marks,
                    updated_at = NOW()
            """
        ),
        {
            "uid": user_id,
            "enabled": resolved.enabled,
            "mode": resolved.mode,
            "model": resolved.model,
            "mining_min": resolved.mining_min_messages,
            "provider": resolved.provider,
            "codex_model": resolved.codex_model,
            "mining_interleave": resolved.mining_interleave,
            "interest_min": resolved.interest_suggest_min_marks,
        },
    )
    return resolved
