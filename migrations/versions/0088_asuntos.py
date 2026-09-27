"""Asuntos (storylines): entidad de primera clase + libro de evidencia + estados + relaciones

Revision ID: 0088_asuntos
Revises: 0087_identidades_orgcore_v3
Create Date: 2026-09-05

Un ASUNTO es un hilo de vida con principio, hitos y fin (un hackathon, el grado, un proyecto) que el
sistema INFIERE de los mensajes (ver docs/modulo-asuntos.md). No se deriva del grafo:
el grafo dice quién está involucrado; el asunto dice qué está pasando, y eso se lee del contenido.

- `mod_asuntos`: la entidad. Ciclo `candidato` (1 sola evidencia, todavía no se muestra) →
  `abierto` → `cerrado` (terminó) | `inactivo` (sin señal N días). `state_text` = estado vigente
  («solo falta inglés»); `aliases` conserva los nombres con que se le llamó (fusiones/renombres).
- `mod_asuntos_evidence`: libro asunto ↔ mensaje (inbox) con método (`llm` | `hilo` | `evento` |
  `asunto` | `manual` | `agent` | `curator`), confianza y, opcionalmente, el HITO fechado que ese
  mensaje marca. La línea de tiempo se lee de aquí.
- `mod_asuntos_states`: historial de estados (auditable: qué dijo el sistema y cuándo).
- `mod_asuntos_relations`: `parte_de` / `requisito_de` / `continua` entre asuntos (Britano →
  requisito_de → inglés → requisito_de → grado).
- `mod_asuntos_seen`: marca «el asignador ya decidió este mensaje» (con o sin asunto) para que el
  job incremental no re-pregunte lo ya visto; el reproceso la borra y vuelve a decidir.
- `mod_asuntos_keys`: llaves deterministas de ADHESIÓN (hilo por Message-ID, evento Luma/ICS,
  asunto normalizado por dominio) para pegar mensajes nuevos a asuntos vivos SIN LLM.
- `asuntos_settings`: una fila por usuario (patrón `identidades_resolver_settings`); sin fila →
  defaults (asignador ENCENDIDO: el módulo se enciende, solo el daemon arranca apagado).

Numeración (migration-numbering-worktrees): 0088 verificado libre en main y en los worktrees/ramas
vivos (relevancia-gate-regex llega a 0077, exp/identidades-calidad a 0082); head lineal =
0087_identidades_orgcore_v3.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0088_asuntos"
down_revision: str | None = "0087_identidades_orgcore_v3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_asuntos (
            id                BIGSERIAL PRIMARY KEY,
            user_id           BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name              TEXT NOT NULL,
            aliases           TEXT[] NOT NULL DEFAULT '{}',
            kind              TEXT NOT NULL
                                CHECK (kind IN ('evento','programa','hackathon','tramite',
                                                'proyecto','comunidad','servicio','otro')),
            description       TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'candidato'
                                CHECK (status IN ('candidato','abierto','cerrado','inactivo')),
            state_text        TEXT NOT NULL DEFAULT '',
            state_updated_at  TIMESTAMPTZ,
            seed              TEXT NOT NULL DEFAULT 'otro',
            entities          TEXT[] NOT NULL DEFAULT '{}',
            opened_by_inbox_id BIGINT,
            first_signal_at   TIMESTAMPTZ,
            last_signal_at    TIMESTAMPTZ,
            evidence_count    INT NOT NULL DEFAULT 0,
            closed_at         TIMESTAMPTZ,
            merged_into_id    BIGINT REFERENCES mod_asuntos(id) ON DELETE SET NULL,
            metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX ix_mod_asuntos_user_status ON mod_asuntos (user_id, status, last_signal_at DESC);
        CREATE INDEX ix_mod_asuntos_merged ON mod_asuntos (merged_into_id) WHERE merged_into_id IS NOT NULL;

        CREATE TABLE mod_asuntos_evidence (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            asunto_id   BIGINT NOT NULL REFERENCES mod_asuntos(id) ON DELETE CASCADE,
            inbox_id    BIGINT NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
            method      TEXT NOT NULL
                          CHECK (method IN ('llm','hilo','evento','asunto','manual','agent','curator')),
            confidence  NUMERIC NOT NULL DEFAULT 1 CHECK (confidence BETWEEN 0 AND 1),
            hito_date   DATE,
            hito_text   TEXT,
            status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','rejected')),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (asunto_id, inbox_id)
        );
        CREATE INDEX ix_mod_asuntos_evidence_inbox ON mod_asuntos_evidence (user_id, inbox_id);
        CREATE INDEX ix_mod_asuntos_evidence_asunto ON mod_asuntos_evidence (asunto_id, status);

        CREATE TABLE mod_asuntos_states (
            id          BIGSERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            asunto_id   BIGINT NOT NULL REFERENCES mod_asuntos(id) ON DELETE CASCADE,
            state_text  TEXT NOT NULL,
            closed      BOOLEAN NOT NULL DEFAULT FALSE,
            source      TEXT NOT NULL DEFAULT 'llm'
                          CHECK (source IN ('llm','curator','manual','agent','system')),
            inbox_id    BIGINT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX ix_mod_asuntos_states_asunto ON mod_asuntos_states (asunto_id, created_at);

        CREATE TABLE mod_asuntos_relations (
            id             BIGSERIAL PRIMARY KEY,
            user_id        BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            src_asunto_id  BIGINT NOT NULL REFERENCES mod_asuntos(id) ON DELETE CASCADE,
            dst_asunto_id  BIGINT NOT NULL REFERENCES mod_asuntos(id) ON DELETE CASCADE,
            relation_type  TEXT NOT NULL CHECK (relation_type IN ('parte_de','requisito_de','continua')),
            confidence     NUMERIC NOT NULL DEFAULT 1 CHECK (confidence BETWEEN 0 AND 1),
            source         TEXT NOT NULL DEFAULT 'llm'
                             CHECK (source IN ('llm','curator','manual','agent')),
            status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','rejected')),
            created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (src_asunto_id <> dst_asunto_id),
            UNIQUE (src_asunto_id, dst_asunto_id, relation_type)
        );

        CREATE TABLE mod_asuntos_keys (
            user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            asunto_id  BIGINT NOT NULL REFERENCES mod_asuntos(id) ON DELETE CASCADE,
            kind       TEXT NOT NULL CHECK (kind IN ('hilo','evento','asunto')),
            key        TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, kind, key)
        );
        CREATE INDEX ix_mod_asuntos_keys_asunto ON mod_asuntos_keys (asunto_id);

        CREATE TABLE mod_asuntos_seen (
            user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            inbox_id    BIGINT NOT NULL REFERENCES inbox(id) ON DELETE CASCADE,
            asunto_id   BIGINT REFERENCES mod_asuntos(id) ON DELETE SET NULL,
            method      TEXT NOT NULL DEFAULT 'llm',
            decided_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (user_id, inbox_id)
        );

        CREATE TABLE asuntos_settings (
            user_id             BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            enabled             BOOLEAN NOT NULL DEFAULT TRUE,
            adhesion_enabled    BOOLEAN NOT NULL DEFAULT TRUE,
            batch_size          INT NOT NULL DEFAULT 40 CHECK (batch_size BETWEEN 1 AND 200),
            body_chars          INT NOT NULL DEFAULT 900 CHECK (body_chars BETWEEN 200 AND 8000),
            closed_window_days  INT NOT NULL DEFAULT 60 CHECK (closed_window_days >= 0),
            inactive_days       INT NOT NULL DEFAULT 60 CHECK (inactive_days >= 1),
            min_evidence_open   INT NOT NULL DEFAULT 2 CHECK (min_evidence_open >= 1),
            min_confidence      NUMERIC NOT NULL DEFAULT 0.6 CHECK (min_confidence BETWEEN 0 AND 1),
            curator_marks       JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS asuntos_settings CASCADE;
        DROP TABLE IF EXISTS mod_asuntos_seen CASCADE;
        DROP TABLE IF EXISTS mod_asuntos_keys CASCADE;
        DROP TABLE IF EXISTS mod_asuntos_relations CASCADE;
        DROP TABLE IF EXISTS mod_asuntos_states CASCADE;
        DROP TABLE IF EXISTS mod_asuntos_evidence CASCADE;
        DROP TABLE IF EXISTS mod_asuntos CASCADE;
        """
    )
