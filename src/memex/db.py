from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, create_engine

from memex.config import settings

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
    return _engine


@contextmanager
def connection() -> Iterator[Connection]:
    """Context-managed connection. Auto-commits on success, rollbacks on exception."""
    with get_engine().begin() as conn:
        yield conn
