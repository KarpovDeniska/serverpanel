"""add Server.ssh_host_key_pub for TOFU pinning

Revision ID: c3d4e5f6a7b8
Revises: b27b27bc40fa
Create Date: 2026-04-21 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b27b27bc40fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("servers") as batch:
        batch.add_column(sa.Column("ssh_host_key_pub", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("servers") as batch:
        batch.drop_column("ssh_host_key_pub")
