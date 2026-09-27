"""identidades: libro de evidencia de vínculos (`mod_identidades_link_claims`) + umbrales de padre

Revision ID: 0086_identidades_link_claims
Revises: 0085_calendar_event_kind
Create Date: 2026-09-05

Hasta acá la jerarquía era «último correo gana»: cada correo que el resolvedor procesaba emitía
«X pertenece a Y» y, si pasaba el umbral, SOBREESCRIBÍA `parent_identity_id` sin dejar de qué
correo salió ni cuántos lo sustentaban; una variación del LLM en un correo pisaba lo que otros
cinco habían dicho, y un padre nombrado que no calzaba exacto creaba OTRA organización. Lo mismo
con las afiliaciones (`person_orgs`): una fila con el último rol.

Esta tabla es el LIBRO: un reclamo por cada afirmación de vínculo, con su procedencia. Nunca se
pisa; el padre efectivo (`parent_identity_id`) y el rol efectivo (`person_orgs.role`) se DERIVAN
del libro (`identidades/links.py`): un padre puesto a mano manda; si no, gana el más sustentado
(mensajes distintos ponderados por confianza), con umbral mínimo (2 mensajes, o 1 con confianza
alta) y estabilidad en empate.

- `kind`: 'pertenece_a' (org/producto → org) | 'afiliado' (persona → org, con `role`).
- `target_id` NULL = padre nombrado que NO se pudo resolver a una identidad existente
  (`target_name` guarda el nombre tal como vino); ya no se inventa una org por eso — lo decide el
  curador o el dueño.
- `method`: llm (resolvedor por correo) | organizer (organizador holístico) | curator (agente
  codex) | manual (UI) | agent (CLI del agente) | sync (contactos del proveedor) | legacy (siembra
  del estado previo al libro). manual/agent/curator PINEAN el padre.
- `inbox_id` sin FK: el correo puede borrarse, la evidencia queda (se muestra «correo borrado»).
- Unicidad por (user, kind, source, target|nombre, correo, método): re-procesar el mismo correo
  no duplica; la confianza se queda con la mayor.

Además, dos umbrales en `identidades_resolver_settings` (perillas del dueño):
`parent_min_messages` (2) y `parent_single_min_confidence` (0.90).

Numeración (migration-numbering-worktrees): 0086 verificado libre en main + worktrees
(relevancia-gate-regex); head lineal = 0085_calendar_event_kind.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0086_identidades_link_claims"
down_revision: str | None = "0085_calendar_event_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mod_identidades_link_claims (
            id               BIGSERIAL PRIMARY KEY,
            user_id          BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind             TEXT NOT NULL CHECK (kind IN ('pertenece_a','afiliado')),
            source_id        BIGINT NOT NULL REFERENCES mod_identidades(id) ON DELETE CASCADE,
            target_id        BIGINT REFERENCES mod_identidades(id) ON DELETE CASCADE,
            target_name      TEXT NOT NULL DEFAULT '',
            target_name_norm TEXT GENERATED ALWAYS AS (memex_norm(target_name)) STORED,
            role             TEXT NOT NULL DEFAULT '',
            role_norm        TEXT GENERATED ALWAYS AS (memex_norm(role)) STORED,
            confidence       NUMERIC(4,3) NOT NULL DEFAULT 1.0
                             CHECK (confidence >= 0 AND confidence <= 1),
            method           TEXT NOT NULL CHECK (method IN
                             ('llm','organizer','curator','manual','agent','sync','legacy')),
            inbox_id         BIGINT,
            status           TEXT NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active','rejected')),
            rationale        TEXT NOT NULL DEFAULT '',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK (target_id IS NOT NULL OR target_name <> ''),
            CHECK (target_id IS NULL OR target_id <> source_id)
        );
        CREATE UNIQUE INDEX mod_identidades_link_claims_uq
            ON mod_identidades_link_claims
               (user_id, kind, source_id, COALESCE(target_id, 0), target_name_norm,
                COALESCE(inbox_id, 0), method);
        CREATE INDEX mod_identidades_link_claims_source
            ON mod_identidades_link_claims (user_id, source_id);
        CREATE INDEX mod_identidades_link_claims_target
            ON mod_identidades_link_claims (user_id, target_id) WHERE target_id IS NOT NULL;
        CREATE INDEX mod_identidades_link_claims_inbox
            ON mod_identidades_link_claims (user_id, inbox_id) WHERE inbox_id IS NOT NULL;

        ALTER TABLE identidades_resolver_settings
            ADD COLUMN parent_min_messages INT NOT NULL DEFAULT 2
                CHECK (parent_min_messages >= 1),
            ADD COLUMN parent_single_min_confidence NUMERIC(4,3) NOT NULL DEFAULT 0.900
                CHECK (parent_single_min_confidence >= 0 AND parent_single_min_confidence <= 1);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE identidades_resolver_settings
            DROP COLUMN IF EXISTS parent_single_min_confidence,
            DROP COLUMN IF EXISTS parent_min_messages;
        DROP TABLE IF EXISTS mod_identidades_link_claims;
        """
    )
