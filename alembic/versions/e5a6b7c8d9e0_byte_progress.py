"""byte-level backup progress + stall threshold

Revision ID: e5a6b7c8d9e0
Revises: d4e5f6a7b8c9
Create Date: 2026-04-23 12:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5a6b7c8d9e0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("backup_history") as batch:
        batch.add_column(sa.Column("bytes_total", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("bytes_done", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("current_item", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("progress_updated_at", sa.DateTime(), nullable=True))

    with op.batch_alter_table("backup_configs") as batch:
        batch.add_column(sa.Column(
            "stall_threshold_seconds",
            sa.Integer(),
            nullable=False,
            server_default="120",
        ))


def downgrade() -> None:
    with op.batch_alter_table("backup_configs") as batch:
        batch.drop_column("stall_threshold_seconds")

    with op.batch_alter_table("backup_history") as batch:
        batch.drop_column("progress_updated_at")
        batch.drop_column("current_item")
        batch.drop_column("bytes_done")
        batch.drop_column("bytes_total")
