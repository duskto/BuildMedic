"""Initial durable tasks, events, artifacts, leases, outbox and validations."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("owner_id", sa.String(80), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("lifecycle", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("lease_token", sa.String(40)),
        sa.Column("heartbeat_at", sa.Float()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_id", "idempotency_key"),
    )
    op.create_index("ix_tasks_owner_id", "tasks", ["owner_id"])
    op.create_index("ix_tasks_lifecycle", "tasks", ["lifecycle"])
    op.create_table(
        "task_events",
        sa.Column("task_id", sa.String(40), primary_key=True),
        sa.Column("seq", sa.Integer(), primary_key=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("task_id", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(40)),
        sa.Column("sha256", sa.String(64)),
        sa.Column("size_bytes", sa.Integer()),
        sa.Column("content_type", sa.String(100)),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float()),
    )
    op.create_index("ix_artifacts_task_id", "artifacts", ["task_id"])
    op.create_table(
        "task_attempts",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("task_id", sa.String(40)),
        sa.Column("lease_token", sa.String(40)),
        sa.Column("started_at", sa.Float()),
        sa.Column("finished_at", sa.Float()),
        sa.Column("reason", sa.String(200)),
    )
    op.create_index("ix_task_attempts_task_id", "task_attempts", ["task_id"])
    op.create_table(
        "outbox",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("task_id", sa.String(40)),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float()),
        sa.Column("published_at", sa.Float()),
    )
    op.create_index("ix_outbox_task_id", "outbox", ["task_id"])
    op.create_table(
        "validations",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("task_id", sa.String(40), nullable=False),
        sa.Column("candidate_id", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
    )
    op.create_index("ix_validations_task_id", "validations", ["task_id"])


def downgrade():
    for table in ("validations", "outbox", "task_attempts", "artifacts", "task_events", "tasks"):
        op.drop_table(table)
