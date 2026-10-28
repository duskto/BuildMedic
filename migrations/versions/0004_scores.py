"""Detached private-behavior receipts, never model inputs or task verdict rewrites."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "experiment_scores",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("experiment_id", sa.String(40), nullable=False, unique=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
    )
    op.create_index("ix_experiment_scores_owner_id", "experiment_scores", ["owner_id"])


def downgrade():
    op.drop_table("experiment_scores")
