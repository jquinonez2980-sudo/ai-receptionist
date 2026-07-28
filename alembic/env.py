from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Resolve the database URL from the DATABASE_URL env var (normalized for the
# psycopg v3 driver) instead of hardcoding it in alembic.ini — alembic.ini is
# tracked in git and must never contain credentials (CLAUDE.md hard rule #1).
# prepend_sys_path = . in alembic.ini puts the repo root on sys.path.
from platform_db import database_url

config = context.config

_url = database_url()
if _url:
    # escape % for configparser interpolation (passwords can contain it)
    config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM metadata yet — migrations are hand-written (see versions/).
target_metadata = None


def _require_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Export it (Railway Postgres public "
            "connection string when running from your machine) before running "
            "alembic."
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode: emit SQL to stdout, no DB needed."""
    context.configure(
        url=_require_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against DATABASE_URL."""
    _require_url()
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
