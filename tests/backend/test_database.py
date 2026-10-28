import sqlite3

import pytest
from sqlalchemy import inspect, text

from buildmedic import db
from buildmedic.database import require_schema, upgrade_database


def test_versioned_0001_database_upgrades_without_losing_tasks():
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from buildmedic import store
    from buildmedic.cases import metadata
    from buildmedic.database import migration_config
    from buildmedic.schemas import CreateTask, Source

    body = CreateTask(source=Source(case_id="pagination")).model_dump()
    tid = store.create("owner", "pre-library-task", body, metadata("pagination"))
    before = store.snapshot(tid)
    with db.engine.begin() as conn:
        command.downgrade(migration_config(conn), "0001")
    assert "experience_versions" not in inspect(db.engine).get_table_names()
    with pytest.raises(RuntimeError):
        require_schema()
    upgrade_database()
    require_schema()
    assert store.snapshot(tid) == before
    with db.engine.connect() as conn:
        assert compare_metadata(MigrationContext.configure(conn), db.metadata) == []


def test_schema_version_is_required_without_implicit_table_creation():
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM alembic_version"))
    with pytest.raises(RuntimeError, match="migrate.py"):
        require_schema()
    with pytest.raises(RuntimeError, match="adopt-existing"):
        upgrade_database()
    require_tables = inspect(db.engine).get_table_names()
    assert "tasks" in require_tables


def test_exact_legacy_schema_is_backed_up_and_adopted():
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(
            db.tasks.insert().values(
                id="legacy-task",
                owner_id="owner",
                idempotency_key="legacy-key",
                request_hash="hash",
                lifecycle="queued",
                version=1,
                created_at=1,
                updated_at=1,
                cancel_requested=False,
                body={},
                snapshot={},
            )
        )
    backup = upgrade_database(adopt_existing=True)
    assert backup and backup.is_file()
    with sqlite3.connect(backup) as copied:
        assert copied.execute("SELECT id FROM tasks").fetchone()[0] == "legacy-task"
    require_schema()
    with db.engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM tasks")).scalar_one() == "legacy-task"


def test_adoption_refuses_schema_drift():
    with db.engine.begin() as conn:
        conn.execute(text("DELETE FROM alembic_version"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN unexpected VARCHAR(20)"))
    with pytest.raises(RuntimeError, match="结构与当前版本不一致"):
        upgrade_database(adopt_existing=True)
    with db.engine.connect() as conn:
        assert not conn.execute(text("SELECT version_num FROM alembic_version")).first()
