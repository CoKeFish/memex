"""identidades: kind canónico `producto` + retiro de `agente` de la taxonomía

Revision ID: 0057_identidades_kind_producto
Revises: 0056_processing_lots
Create Date: 2026-06-10

La taxonomía de clasificación queda en persona | organizacion | producto (`unknown` sigue siendo
SOLO el escape del extractor, a nivel de mención). Hasta ahora el directorio plegaba
producto/agente → 'organizacion' (decisión de 0033, ver su docstring); ahora `producto` es kind
canónico de `mod_identidades` y de `resolved_kind`, y `agente` SALE de la taxonomía: las menciones
existentes migran a 'producto' (un "agente" era una herramienta/IA — semánticamente un producto) y
el CHECK de `mentioned_kind` deja de admitirlo.

También se amplía el índice parcial trigram de `org_core` a los productos: el dedup difuso
(fuzzy.py) matchea por `org_core` todo kind ≠ persona, y los productos quedan kind-scoped entre sí.

DESPLIEGUE: aplicar junto con el código que deja de ofrecer 'agente' en el extractor — con esta
migración aplicada y el código viejo corriendo, una mención 'agente' violaría el CHECK nuevo.

DOWNGRADE (lossy): re-pliega kind/resolved_kind 'producto' → 'organizacion' ANTES de estrechar los
CHECKs; las menciones ex-'agente' QUEDAN como 'producto' (valor ya válido pre-0057): 'agente' no se
restaura. Si el backfill de reclasificación corrió, las aristas/membresías con slug
'identidades:producto' quedan apuntando a un vértice que ya no proyecta y la poda del grafo las
barre en el siguiente build.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0057_identidades_kind_producto"
down_revision: str | None = "0056_processing_lots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1/2. El directorio y el kind resuelto admiten 'producto'. Los CHECK de 0033 son inline sin
    #      nombre → autonombres de Postgres (verificados contra la DB); de aquí en adelante quedan
    #      con nombre explícito.
    op.execute(
        """
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
        """
    )
    # 3/4. 'agente' sale de la taxonomía: migrar las filas ANTES de reescribir el CHECK (el ADD
    #      valida las filas existentes).
    op.execute(
        """
        UPDATE mod_identidades_mentions SET mentioned_kind = 'producto'
            WHERE mentioned_kind = 'agente';
        ALTER TABLE mod_identidades_mentions
            DROP CONSTRAINT IF EXISTS mod_identidades_mentions_mentioned_kind_check;
        ALTER TABLE mod_identidades_mentions
            ADD CONSTRAINT mod_identidades_mentions_mentioned_kind_check
            CHECK (mentioned_kind IN ('persona','organizacion','producto','unknown'));
        """
    )
    # 5. Trigram de org_core también para productos.
    op.execute(
        """
        DROP INDEX IF EXISTS mod_identidades_orgcore_trgm;
        CREATE INDEX mod_identidades_orgcore_trgm ON mod_identidades
            USING GIN (org_core gin_trgm_ops)
            WHERE kind IN ('organizacion','producto');
        """
    )


def downgrade() -> None:
    # Re-pliegue lossy (ver docstring): los datos se mueven ANTES de estrechar los CHECKs.
    op.execute(
        """
        UPDATE mod_identidades SET kind = 'organizacion' WHERE kind = 'producto';
        UPDATE mod_identidades_mentions SET resolved_kind = 'organizacion'
            WHERE resolved_kind = 'producto';
        ALTER TABLE mod_identidades
            DROP CONSTRAINT IF EXISTS mod_identidades_kind_check;
        ALTER TABLE mod_identidades
            ADD CONSTRAINT mod_identidades_kind_check
            CHECK (kind IN ('persona','organizacion'));
        ALTER TABLE mod_identidades_mentions
            DROP CONSTRAINT IF EXISTS mod_identidades_mentions_resolved_kind_check;
        ALTER TABLE mod_identidades_mentions
            ADD CONSTRAINT mod_identidades_mentions_resolved_kind_check
            CHECK (resolved_kind IN ('persona','organizacion'));
        ALTER TABLE mod_identidades_mentions
            DROP CONSTRAINT IF EXISTS mod_identidades_mentions_mentioned_kind_check;
        ALTER TABLE mod_identidades_mentions
            ADD CONSTRAINT mod_identidades_mentions_mentioned_kind_check
            CHECK (mentioned_kind IN ('persona','organizacion','producto','agente','unknown'));
        DROP INDEX IF EXISTS mod_identidades_orgcore_trgm;
        CREATE INDEX mod_identidades_orgcore_trgm ON mod_identidades
            USING GIN (org_core gin_trgm_ops)
            WHERE kind = 'organizacion';
        """
    )
