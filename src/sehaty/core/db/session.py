"""Lazy engine + session factory.

The engine is built on first use from the `DATABASE_URL` env var, so this
module is importable without a live database (tests import `sehaty.core`
freely). Services use `with get_session() as s:` for their IO.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from sehaty.core.errors import SehatyError

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _get_session_factory() -> sessionmaker[Session]:
    """Build (once) and return the process-wide session factory."""
    global _engine, _session_factory
    if _session_factory is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise SehatyError("DATABASE_URL is not set")
        _engine = create_engine(url, pool_pre_ping=True, future=True)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _session_factory


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a session, committing on success and rolling back on error."""
    session = _get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
