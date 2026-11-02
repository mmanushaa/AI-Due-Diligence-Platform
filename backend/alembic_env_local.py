"""Local-only alembic bootstrap so `alembic upgrade head` works both in the
container (WORKDIR /srv, no alembic.ini shipped) and on the host (script
location is backend/migrations)."""

from pathlib import Path
from alembic.config import Config
from alembic import context

HERE = Path(__file__).resolve().parent

# Pick the right alembic.ini depending on where we are running.
if (HERE / "alembic.ini").exists():
    cfg = Config(str(HERE / "alembic.ini"))
else:
    # Running from a container build dir without alembic.ini; point at the
    # mounted host copy under /host.
    host_ini = Path("/host/backend/migrations/alembic.ini")
    cfg = Config(str(host_ini)) if host_ini.exists() else Config()

cfg.set_main_option(
    "script_location",
    str(HERE / "migrations") if (HERE / "migrations").exists() else cfg.get_main_option("script_location"),
)
if not cfg.get_main_option("sqlalchemy.url"):
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://postgres:postgres@postgres:5432/due_diligence")

from app.core.config import settings
cfg.set_main_option("sqlalchemy.url", settings.database_url)

from app.db.base import Base
target_metadata = Base.metadata

from logging.config import fileConfig
if cfg.config_file_name:
    fileConfig(cfg.config_file_name)

def run_migrations_offline() -> None:
    context.configure(url=settings.database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    from sqlalchemy import create_engine
    connectable = create_engine(settings.database_url, pool_pre_ping=True)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
