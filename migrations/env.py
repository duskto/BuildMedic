from alembic import context

from buildmedic.config import settings
from buildmedic.db import make_engine, metadata

config = context.config
if context.is_offline_mode():
    context.configure(
        url=settings().database_url,
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:

    def migrate(connection):
        context.configure(connection=connection, target_metadata=metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()

    connection = config.attributes.get("connection")
    if connection is not None:
        migrate(connection)
    else:
        engine = make_engine()
        with engine.connect() as connection:
            migrate(connection)
