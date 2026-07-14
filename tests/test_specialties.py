"""Specialties-catalogue core tests on an in-memory SQLite engine.

The ``specialties`` table has no PostGIS column, so — unlike ``test_admin`` /
``test_auth`` — no dialect compilation shim is needed; we create only
``Specialty.__table__`` on the throwaway engine.
"""

import pytest
from sehaty.db import Specialty
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.specialties import (
    _DEFAULT_SPECIALTIES,
    SpecialtyController,
)
from sehaty.core.db import session as session_mod

_TABLES = [Specialty.__table__]


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _count(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return s.execute(select(func.count()).select_from(Specialty)).scalar_one()


def test_seed_defaults_inserts_full_catalogue(db: sessionmaker[Session]) -> None:
    inserted = SpecialtyController.seed_defaults()

    assert inserted == len(_DEFAULT_SPECIALTIES)
    assert _count(db) == len(_DEFAULT_SPECIALTIES)


def test_seed_defaults_is_idempotent(db: sessionmaker[Session]) -> None:
    first = SpecialtyController.seed_defaults()
    baseline = _count(db)

    second = SpecialtyController.seed_defaults()

    assert first == len(_DEFAULT_SPECIALTIES)
    assert second == 0  # nothing new the second time round
    assert _count(db) == baseline  # count unchanged -> no duplicates


def test_list_specialties_ordered_and_localized(db: sessionmaker[Session]) -> None:
    SpecialtyController.seed_defaults()

    rows = SpecialtyController.list_specialties()

    assert len(rows) == len(_DEFAULT_SPECIALTIES)
    # Ordered by English name.
    names_en = [r.name_en for r in rows]
    assert names_en == sorted(names_en)
    # Every row carries all three localized names + a slug.
    for r in rows:
        assert r.slug
        assert r.name_en and r.name_fr and r.name_ar


def test_list_specialties_empty_when_unseeded(db: sessionmaker[Session]) -> None:
    assert SpecialtyController.list_specialties() == []
