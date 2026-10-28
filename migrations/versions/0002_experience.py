"""Owner-scoped immutable experience versions and frozen task inputs."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "experience_versions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("domain", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_task_id", sa.String(40), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_id", "domain", "version"),
        sa.UniqueConstraint("owner_id", "idempotency_key"),
    )
    op.create_index("ix_experience_versions_owner_id", "experience_versions", ["owner_id"])
    op.create_table(
        "experience_snapshots",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("task_id", sa.String(40), nullable=False, unique=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
    )
    op.create_index("ix_experience_snapshots_owner_id", "experience_snapshots", ["owner_id"])


def downgrade():
    op.drop_table("experience_snapshots")
    op.drop_table("experience_versions")
