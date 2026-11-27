"""writer progress (replay protection for the event writer)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-26 20:00:00+00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "writer_progress",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("epoch", sa.String(length=36), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_writer_progress")),
    )


def downgrade() -> None:
    op.drop_table("writer_progress")
