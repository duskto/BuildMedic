"""Owner-scoped, bounded cache of sanitized completed GitHub Job logs."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ci_log_cache",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("binding", sa.JSON(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
    )
    op.create_index("ix_ci_log_cache_owner_id", "ci_log_cache", ["owner_id"])
    op.create_index("ix_ci_log_cache_expires_at", "ci_log_cache", ["expires_at"])


def downgrade():
    op.drop_table("ci_log_cache")
