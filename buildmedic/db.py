import time
from contextlib import contextmanager

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)

from .config import settings

metadata = MetaData()
tasks = Table(
    "tasks",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("owner_id", String(80), nullable=False, index=True),
    Column("idempotency_key", String(128), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("lifecycle", String(20), nullable=False, index=True),
    Column("version", Integer, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("lease_token", String(40)),
    Column("heartbeat_at", Float),
    Column("cancel_requested", Boolean, nullable=False, default=False),
    Column("body", JSON, nullable=False),
    Column("snapshot", JSON, nullable=False),
    UniqueConstraint("owner_id", "idempotency_key"),
)
events = Table(
    "task_events",
    metadata,
    Column("task_id", String(40), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("event_type", String(50), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
)
artifacts = Table(
    "artifacts",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("task_id", String(40), index=True, nullable=False),
    Column("kind", String(40)),
    Column("sha256", String(64)),
    Column("size_bytes", Integer),
    Column("content_type", String(100)),
    Column("content", Text, nullable=False),
    Column("created_at", Float),
)
attempts = Table(
    "task_attempts",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("task_id", String(40), index=True),
    Column("lease_token", String(40)),
    Column("started_at", Float),
    Column("finished_at", Float),
    Column("reason", String(200)),
)
outbox = Table(
    "outbox",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("task_id", String(40), index=True),
    Column("kind", String(20), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", Float),
    Column("published_at", Float),
)
validations = Table(
    "validations",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("task_id", String(40), index=True, nullable=False),
    Column("candidate_id", String(40), nullable=False),
    Column("status", String(20), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("result", JSON, nullable=False),
)
experience_versions = Table(
    "experience_versions",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("owner_id", String(80), nullable=False, index=True),
    Column("domain", String(100), nullable=False),
    Column("version", Integer, nullable=False),
    Column("source_task_id", String(40), nullable=False, unique=True),
    Column("idempotency_key", String(128), nullable=False),
    Column("status", String(20), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("content", JSON, nullable=False),
    UniqueConstraint("owner_id", "domain", "version"),
    UniqueConstraint("owner_id", "idempotency_key"),
)
experience_snapshots = Table(
    "experience_snapshots",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("owner_id", String(80), nullable=False, index=True),
    Column("task_id", String(40), nullable=False, unique=True),
    Column("created_at", Float, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("content", JSON, nullable=False),
)
experiments = Table(
    "experiments",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("owner_id", String(80), nullable=False, index=True),
    Column("idempotency_key", String(128), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("status", String(20), nullable=False),
    Column("content", JSON, nullable=False),
    UniqueConstraint("owner_id", "idempotency_key"),
)


experiment_scores = Table(
    "experiment_scores",
    metadata,
    Column("id", String(40), primary_key=True),
    Column("experiment_id", String(40), nullable=False, unique=True),
    Column("owner_id", String(80), nullable=False, index=True),
    Column("status", String(20), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("content", JSON, nullable=False),
)


def make_engine(url=None):
    settings().data_dir.mkdir(parents=True, exist_ok=True)
    url = url or settings().database_url
    eng = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {},
        pool_pre_ping=True,
    )
    if url.startswith("sqlite"):

        @event.listens_for(eng, "connect")
        def pragmas(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")

    return eng


engine = make_engine()


@contextmanager
def transaction():
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            conn.begin()
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def init_db():
    metadata.create_all(engine)


def now():
    return time.time()
