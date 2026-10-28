"""Versioned schema setup, including explicit adoption of an old create_all database."""

import sqlite3
import time

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from . import db
from .config import ROOT, settings


def migration_config(connection=None):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def require_schema():
    expected = set(ScriptDirectory.from_config(migration_config()).get_heads())
    with db.engine.connect() as conn:
        current = set(MigrationContext.configure(conn).get_current_heads())
    if current != expected:
        raise RuntimeError("数据库版本未就绪，请先运行 uv run python scripts/migrate.py")


def upgrade_database(adopt_existing=False):
    backup = None
    with db.engine.connect() as conn:
        context = MigrationContext.configure(conn)
        tables = set(inspect(conn).get_table_names()) - {"alembic_version"}
        if tables and not context.get_current_heads():
            if not adopt_existing:
                raise RuntimeError("已有未登记版本的数据库；请备份后运行 scripts/migrate.py --adopt-existing")
            # Adoption is allowed only if every table/column/index matches the current head.
            if compare_metadata(context, db.metadata):
                raise RuntimeError("已有数据库结构与当前版本不一致，已停止接管；请人工迁移")
            if db.engine.dialect.name == "sqlite":
                database = db.engine.url.database
                if database and database != ":memory:":
                    dest = settings().data_dir / "backups" / f"pre-adoption-{time.time_ns()}.sqlite3"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with sqlite3.connect(database) as source, sqlite3.connect(dest) as target:
                        source.backup(target)
                    dest.chmod(0o600)
                    backup = dest
            conn.commit()
            command.stamp(migration_config(conn), "head")
        conn.commit()
        command.upgrade(migration_config(conn), "head")
        conn.commit()
    return backup
