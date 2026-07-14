"""Pure-SQLite tests for ``DoctorController.upsert_profile`` language persistence.

The public view round-trip (``get_by_slug``) is covered against a live PostGIS
in ``test_doctor_profile.py``; that path needs ``ST_X``/``ST_Y`` which stock
SQLite cannot compile. Persisting ``languages`` onto the profile row, however,
touches no geography function, so we exercise it here on a throwaway in-memory
SQLite engine.

Like the other SQLite suites (``test_admin``/``test_auth``/…), the PostGIS
``Geography`` column is not buildable on stock SQLite, so we register a
dialect-scoped shim mapping it to ``TEXT`` purely for the test engine. No
``lat``/``lng`` is ever passed, so ``geopoint`` stays NULL and is never read.
"""

import pytest
from geoalchemy2 import Geography
from geoalchemy2 import functions as geo_functions
from sehaty.db import DoctorProfile, DoctorSpecialty, Specialty, User, UserRole
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.doctors import DoctorController
from sehaty.core.db import session as session_mod


@compiles(Geography, "sqlite")
def _geography_as_text_on_sqlite(element, compiler, **kw) -> str:
    # SQLite has no geography type; store the column as opaque TEXT for tests.
    return "TEXT"


@compiles(geo_functions.ST_GeogFromText, "sqlite")
def _geog_bind_passthrough_on_sqlite(element, compiler, **kw) -> str:
    # Skip the PostGIS constructor SQLite lacks; bind the raw value instead.
    return compiler.process(list(element.clauses)[0], **kw)


@compiles(geo_functions.ST_AsEWKB, "sqlite")
@compiles(geo_functions.ST_AsBinary, "sqlite")
def _read_geopoint_passthrough_on_sqlite(element, compiler, **kw) -> str:
    # ``upsert_profile`` loads the whole ``DoctorProfile`` entity, and
    # GeoAlchemy2 wraps geography reads in ``ST_AsEWKB``/``ST_AsBinary`` (which
    # SQLite lacks). We never write a point in these tests, so ``geopoint`` is
    # NULL; return the raw column and let the result processor yield ``None``.
    return compiler.process(list(element.clauses)[0], **kw)


_TABLES = [
    User.__table__,
    DoctorProfile.__table__,
    Specialty.__table__,
    DoctorSpecialty.__table__,
]


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


def _make_doctor(factory: sessionmaker[Session], email: str) -> int:
    with factory() as s:
        user = User(email=email, role=UserRole.DOCTOR, is_active=True)
        s.add(user)
        s.commit()
        return user.id


def _stored_languages(factory: sessionmaker[Session], user_id: int) -> list[str]:
    with factory() as s:
        return s.execute(
            select(DoctorProfile.languages).where(DoctorProfile.user_id == user_id)
        ).scalar_one()


def test_create_persists_languages(db: sessionmaker[Session]) -> None:
    uid = _make_doctor(db, "langs@clinic.ma")

    DoctorController.upsert_profile(uid, full_name="Dr Langs", languages=["ar", "fr"])

    assert _stored_languages(db, uid) == ["ar", "fr"]


def test_create_defaults_to_empty_list(db: sessionmaker[Session]) -> None:
    uid = _make_doctor(db, "nolangs@clinic.ma")

    # No languages given on create -> stored as an empty list, never NULL.
    DoctorController.upsert_profile(uid, full_name="Dr No Langs")

    assert _stored_languages(db, uid) == []


def test_update_none_retains_existing(db: sessionmaker[Session]) -> None:
    uid = _make_doctor(db, "retain@clinic.ma")
    DoctorController.upsert_profile(uid, full_name="Dr Retain", languages=["ar", "fr"])

    # A later update that omits languages (None) must not clobber the stored list.
    DoctorController.upsert_profile(uid, full_name="Dr Retain", bio="updated")

    assert _stored_languages(db, uid) == ["ar", "fr"]


def test_update_list_replaces(db: sessionmaker[Session]) -> None:
    uid = _make_doctor(db, "replace@clinic.ma")
    DoctorController.upsert_profile(uid, full_name="Dr Replace", languages=["ar", "fr"])

    # A list (even a different one) fully replaces the stored languages.
    DoctorController.upsert_profile(uid, full_name="Dr Replace", languages=["en"])

    assert _stored_languages(db, uid) == ["en"]
