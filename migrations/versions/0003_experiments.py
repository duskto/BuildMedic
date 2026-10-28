"""Preregistered development comparisons with frozen execution inputs."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "experiments",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_id", "idempotency_key"),
    )
    op.create_index("ix_experiments_owner_id", "experiments", ["owner_id"])


def downgrade():
    op.drop_table("experiments")
