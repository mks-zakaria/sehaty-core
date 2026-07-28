"""Shared pytest fixtures.

Most core tests run against a throwaway in-memory SQLite engine (see the
per-module ``db`` fixtures). The doctor-profile suite is different: it exercises
the real PostGIS ``geopoint`` column (``WKTElement`` writes, ``ST_X``/``ST_Y``
reads), which stock SQLite cannot compile. Those tests take the ``pg_session``
fixture, which binds the core session factory to a live PostGIS reachable at
``DATABASE_URL`` (defaulting to the local Docker container) and SKIPs cleanly
when no database is available — so the unit suite still runs anywhere.
"""

import os
import subprocess

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from sehaty.core.db import session as session_mod

_DEFAULT_DATABASE_URL = "postgresql+psycopg://sehaty:sehaty@localhost:5432/sehaty"


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", _DEFAULT_DATABASE_URL)


@pytest.fixture(scope="session")
def _pg_engine():
    """Session-wide PostGIS engine; migrated once, or SKIP if unreachable."""
    url = _database_url()
    engine = create_engine(url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"PostGIS not reachable at {url}: {exc}")

    # Apply migrations once (idempotent). The console script ships with the
    # sehaty-db dependency and reads DATABASE_URL itself.
    result = subprocess.run(
        ["sehaty-migrate"],
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:  # pragma: no cover - environment-dependent
        pytest.skip(f"sehaty-migrate failed:\n{result.stdout}\n{result.stderr}")

    yield engine
    engine.dispose()


@pytest.fixture
def pg_session(_pg_engine) -> Session:
    """A clean PostGIS session with the core factory pointed at it.

    Truncates the doctor/user/specialty tables (CASCADE) before each test so
    controllers — which open their own sessions via ``get_session()`` — see a
    blank slate, then yields a raw session for direct setup and assertions.

    ``plans`` is truncated explicitly: it has no foreign key to ``users``, so
    CASCADE never reaches it, and a plan code written by one run collided with
    the next. That only ever bit locally — CI builds a fresh database per run,
    so the suite was green there while failing on a second local run.
    """
    factory = sessionmaker(bind=_pg_engine, expire_on_commit=False)
    with factory() as cleaner:
        cleaner.execute(text("TRUNCATE users, specialties, plans RESTART IDENTITY CASCADE"))
        cleaner.commit()

    session_mod.set_session_factory(factory)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        session_mod.set_session_factory(None)
