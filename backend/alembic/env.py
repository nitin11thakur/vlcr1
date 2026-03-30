"""
alembic/env.py
--------------
Async Alembic environment using asyncpg / SQLAlchemy async engine.
The DATABASE_URL is pulled from app.core.config.settings so there is
a single source of truth — no duplication in alembic.ini.
"""

import asyncio
import os
import sys
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# ── FIX: Ensure 'backend/' is on sys.path so 'from app...' imports work
# regardless of the working directory from which alembic is invoked.
# __file__ is backend/alembic/env.py → parent is backend/alembic/ → parent.parent is backend/
_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

# ── Import Base and all models so Alembic sees the full metadata ───────────────
from app.core.database import Base  # noqa: F401
import app.models.models  # noqa: F401 — registers all ORM models on Base.metadata

from app.core.config import settings

# ── Alembic Config object (gives access to alembic.ini values) ────────────────
config = context.config

# Override sqlalchemy.url from application settings (single source of truth).
# The async engine needs +asyncpg; the offline/sync path strips it below.
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# Interpret the config file for Python logging if present
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for 'autogenerate' support
target_metadata = Base.metadata


# ── Offline migrations (generate SQL without a live DB connection) ─────────────
def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (generates SQL without connecting to DB)."""
    # Strip +asyncpg for offline mode — dialect_opts uses psycopg2 syntax
    url = config.get_main_option("sqlalchemy.url")
    url = url.replace("+asyncpg", "")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online migrations (async) ─────────────────────────────────────────────────
def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations inside a sync wrapper."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online (async) migrations."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
