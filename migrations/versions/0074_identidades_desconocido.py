"""identidades: kind canónico `desconocido` (estado «pendiente de clasificación»)

Cuarto kind canónico, transversal como `producto` en 0057. La entidad EXISTE (sabemos que hay algo
en ese correo/handle) pero su TIPO queda `desconocido` hasta que un sistema lo defina (set-kind
manual, un clasificador futuro, o una extracción posterior que lo afirme). Reemplaza el «default a
persona ante la duda» de la resolución de remitente: un buzón de dominio corporativo con local-part
ambiguo (ni rol/genérico ni claramente una persona — `ielec@`, `viceacad@`) ya NO se adivina como
persona; se crea `desconocido` (y se le teje igual la afiliación a la org del dominio).

`desconocido` es kind canónico de `mod_identidades.kind` y de `mod_identidades_mentions`
.resolved_kind. NO se agrega a `mentioned_kind` (el vocabulario del EXTRACTOR sigue siendo
persona|organizacion|producto|unknown; el avistamiento de un remitente desconocido registra
`mentioned_kind='unknown'` y `resolved_kind='desconocido'`). El índice trigram parcial de `org_core`
se amplía a `desconocido` (el dedup difuso de `fuzzy.py` matchea por `org_core` todo kind ≠ persona).

DESPLIEGUE: aplicar junto con el código que crea/resuelve `desconocido` — con esta migración
aplicada y el código viejo corriendo no pasa nada (nadie escribe `desconocido` aún); con el código
nuevo y esta migración SIN aplicar, un INSERT/UPDATE a `desconocido` violaría el CHECK.

DOWNGRADE (lossy): re-pliega kind/resolved_kind 'desconocido' → 'persona' (el estado pre-0074, donde
estos remitentes se adivinaban persona) ANTES de estrechar los CHECKs, y restablece el índice sin
'desconocido'. El tipo «pendiente» no se restaura: la información de que era incierto se pierde.

Numeración (migration-numbering-worktrees): 0074 verificado libre en los 3 worktrees y todas las
ramas (head = 0073). down_revision = head.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0074_identidades_desconocido"  # <= 32 chars (alembic_version.version_num)
down_revision: str | None = "0073_identidades_orgcore_sin_spa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1/2. El directorio y el kind resuelto admiten 'desconocido' (mentioned_kind NO cambia).
    op.execute(
        """
        ALTER TABLE mod_identidades
            DROP CONSTRAINT IF EXISTS mod_identidades_kind_check;
        ALTER TABLE mod_identidades
            ADD CONSTRAINT mod_identidades_kind_check
            CHECK (kind IN ('persona','organizacion','producto','desconocido'));
        ALTER TABLE mod_identidades_mentions
            DROP CONSTRAINT IF EXISTS mod_identidades_mentions_resolved_kind_check;
        ALTER TABLE mod_identidades_mentions
            ADD CONSTRAINT mod_identidades_mentions_resolved_kind_check
            CHECK (resolved_kind IN ('persona','organizacion','producto','desconocido'));
        """
    )
    # 3. Trigram de org_core también para 'desconocido' (fuzzy.py lo dedupea por org_core, como a las
    #    orgs/productos: col = org_core para todo kind ≠ persona).
    op.execute(
        """
        DROP INDEX IF EXISTS mod_identidades_orgcore_trgm;
        CREATE INDEX mod_identidades_orgcore_trgm ON mod_identidades
            USING GIN (org_core gin_trgm_ops)
            WHERE kind IN ('organizacion','producto','desconocido');
        """
    )


def downgrade() -> None:
    # Re-pliegue lossy (ver docstring): 'desconocido' → 'persona' ANTES de estrechar los CHECKs.
    op.execute(
        """
        UPDATE mod_identidades SET kind = 'persona' WHERE kind = 'desconocido';
        UPDATE mod_identidades_mentions SET resolved_kind = 'persona'
            WHERE resolved_kind = 'desconocido';
        ALTER TABLE mod_identidades
            DROP CONSTRAINT IF EXISTS mod_identidades_kind_check;
        ALTER TABLE mod_identidades
            ADD CONSTRAINT mod_identidades_kind_check
            CHECK (kind IN ('persona','organizacion','producto'));
        ALTER TABLE mod_identidades_mentions
            DROP CONSTRAINT IF EXISTS mod_identidades_mentions_resolved_kind_check;
        ALTER TABLE mod_identidades_mentions
            ADD CONSTRAINT mod_identidades_mentions_resolved_kind_check
            CHECK (resolved_kind IN ('persona','organizacion','producto'));
        DROP INDEX IF EXISTS mod_identidades_orgcore_trgm;
        CREATE INDEX mod_identidades_orgcore_trgm ON mod_identidades
            USING GIN (org_core gin_trgm_ops)
            WHERE kind IN ('organizacion','producto');
        """
    )
