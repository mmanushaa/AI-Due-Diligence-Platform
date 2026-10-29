from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.core.config import settings

_engine: Engine | None = None


def get_engine() -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 3},
        )
    return _engine
