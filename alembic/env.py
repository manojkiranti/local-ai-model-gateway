"""Alembic environment — async engine, URL + metadata pulled from the app.

We import the app's settings (for the DB URL) and the declarative Base plus the
model modules so autogenerate sees every table.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.db.base import Base

# Import model modules so their tables register on Base.metadata.
from app.users import models as _users_models  # noqa: F401
from app.history import models as _history_models  # noqa: F401
from app.files import models as _files_models  # noqa: F401
from app.rag import models as _rag_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the runtime DB URL (no credentials in alembic.ini).
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata

# Indexes whose PostgreSQL-specific options Alembic cannot round-trip: it does
# not reliably reflect an HNSW operator class or its WITH (m, ef_construction)
# build parameters, so a drift check would propose dropping and recreating them
# on every run. They ARE declared on the model (so autogenerate knows they
# exist) and created by hand in the migration; this only excludes them from
# comparison.
_AUTOGEN_SKIP_INDEXES = {"ix_chunks_embedding", "ix_chunks_tsv"}


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "index" and name in _AUTOGEN_SKIP_INDEXES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
