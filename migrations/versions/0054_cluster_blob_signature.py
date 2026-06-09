"""relation_clusters.blob_signature: separa la identidad de DETECCIÓN de la identidad del CÚMULO

Revision ID: 0054_cluster_blob_signature
Revises: 0053_relation_clusters
Create Date: 2026-06-09

El validador pasó de PORTERO (1 blob → 1 cúmulo keep/reject) a PARTIDOR (1 blob → N contextos): el LLM
parte un blob detectado en los contextos coherentes que tenga. Para persistir eso sin perder la
identidad de cada cúmulo cuando el blob deriva (ingesta aditiva), un cúmulo lleva DOS firmas:

- `signature` = hash del set de miembros de ESTE row (el GRUPO, para un confirmed; el BLOB entero,
  para un candidate/rejected). Es la identidad propia del cúmulo.
- `blob_signature` = hash del BLOB detectado del que salió. Varios hijos `confirmed` comparten el
  `blob_signature` de su blob padre. Es la clave de "¿este blob detectado ya fue particionado?": si
  hay rows con ese `blob_signature` en (confirmed/stale/rejected), el blob está manejado (estable).

Backfill: los confirmed pre-partición eran 1-a-1 con su blob → `blob_signature = signature`. El índice
único PARCIAL sobre `signature WHERE status IN (candidate,rejected)` se conserva intacto (esos rows
siguen con `signature = blob_signature`). El índice nuevo `(user_id, blob_signature)` es NO único (los
hijos comparten blob_signature a propósito).

Numeración (migration-numbering-worktrees): 0054 verificado libre en todos los worktrees; head = 0053.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0054_cluster_blob_signature"
down_revision: str | None = "0053_relation_clusters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE relation_clusters ADD COLUMN blob_signature CHAR(64);
        UPDATE relation_clusters SET blob_signature = signature WHERE blob_signature IS NULL;
        ALTER TABLE relation_clusters ALTER COLUMN blob_signature SET NOT NULL;
        CREATE INDEX relation_clusters_blob ON relation_clusters (user_id, blob_signature);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS relation_clusters_blob;
        ALTER TABLE relation_clusters DROP COLUMN IF EXISTS blob_signature;
        """
    )
