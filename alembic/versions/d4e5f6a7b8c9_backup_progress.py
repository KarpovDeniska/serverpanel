"""add BackupHistory.current_step + progress

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-22 12:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("backup_history") as batch:
        batch.add_column(sa.Column("current_step", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("progress", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table("backup_history") as batch:
        batch.drop_column("progress")
        batch.drop_column("current_step")
