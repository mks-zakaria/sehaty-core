"""Daily patient-cap tests (CAP exception) on an in-memory SQLite engine.

A CAP availability exception limits a date to ``max_patients`` bookings: once
reached, that day's slots vanish and further bookings are rejected. Also covers
the new in-place edit paths on both availability controllers.
"""

from datetime import UTC, date, datetime, time

import pytest
from geoalchemy2 import Geography
from geoalchemy2 import functions as geo_functions
from sehaty.db import (
    Appointment,
    AuditLog,
    Availability,
    AvailabilityException,
    ClinicPatient,
    DoctorProfile,
    Notification,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.appointments import AppointmentController
from sehaty.core.controllers.availability import AvailabilityController
from sehaty.core.controllers.availability_exceptions import AvailabilityExceptionController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyConflictError, SehatyValidationError
from sehaty.core.services.slots import available_slots, daily_cap_reached


@compiles(Geography, "sqlite")
def _compile_geography_sqlite(type_, compiler, **kw) -> str:  # noqa: ANN001
    return "TEXT"


@compiles(geo_functions.ST_GeogFromText, "sqlite")
def _geog_bind_passthrough_on_sqlite(element, compiler, **kw) -> str:  # noqa: ANN001
    return compiler.process(list(element.clauses)[0], **kw)


_TABLES = [
    User.__table__,
    DoctorProfile.__table__,
    Availability.__table__,
    AvailabilityException.__table__,
    ClinicPatient.__table__,
    Appointment.__table__,
    AuditLog.__table__,
    Notification.__table__,
]

_MONDAY = date(2026, 8, 3)
assert _MONDAY.weekday() == 0


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _seed_doctor(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        doc = User(email="doc@clinic.ma", role=UserRole.DOCTOR, is_active=True)
        s.add(doc)
        s.commit()
        doc_id = doc.id
        s.add(
            DoctorProfile(
                user_id=doc_id, full_name="Dr Test", slug="dr-test", license_no="L1", timezone="UTC"
            )
        )
        s.commit()
    return doc_id


def _seed_patient(factory: sessionmaker[Session], email: str) -> int:
    with factory() as s:
        p = User(email=email, role=UserRole.PATIENT, is_active=True)
        s.add(p)
        s.commit()
        return p.id


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime.combine(_MONDAY, time(hour, minute), tzinfo=UTC)


def test_cap_suppresses_day_once_full(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)  # 6 slots
    AvailabilityExceptionController.add(doc, _MONDAY, "CAP", max_patients=2)

    p1 = _seed_patient(db, "p1@x.ma")
    p2 = _seed_patient(db, "p2@x.ma")
    p3 = _seed_patient(db, "p3@x.ma")

    AppointmentController.book(p1, doc, _at(9, 0))
    # One booked, cap 2 → day still open.
    with db() as s:
        assert available_slots(s, doc, _MONDAY, _MONDAY)
    AppointmentController.book(p2, doc, _at(9, 30))

    # Cap reached: that day's slots vanish and a third booking is rejected.
    with db() as s:
        assert available_slots(s, doc, _MONDAY, _MONDAY) == []
    with pytest.raises(SehatyConflictError):
        AppointmentController.book(p3, doc, _at(10, 0))


def test_no_cap_allows_more(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)
    for i, hm in enumerate([(9, 0), (9, 30), (10, 0)]):
        p = _seed_patient(db, f"n{i}@x.ma")
        AppointmentController.book(p, doc, _at(*hm))
    # Three booked, no cap → slots still remain that day.
    with db() as s:
        assert available_slots(s, doc, _MONDAY, _MONDAY)


def test_daily_cap_reached_helper(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)
    AvailabilityExceptionController.add(doc, _MONDAY, "CAP", max_patients=1)
    p1 = _seed_patient(db, "h1@x.ma")

    with db() as s:
        assert daily_cap_reached(s, doc, _at(9, 0)) is False
    AppointmentController.book(p1, doc, _at(9, 0))
    with db() as s:
        assert daily_cap_reached(s, doc, _at(10, 0)) is True


def test_cap_requires_positive_max(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        AvailabilityExceptionController.add(doc, _MONDAY, "CAP")


def test_update_exception_to_cap(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    row = AvailabilityExceptionController.add(
        doc, _MONDAY, "OPEN", start_time=time(9, 0), end_time=time(10, 0), slot_minutes=30
    )
    updated = AvailabilityExceptionController.update(doc, row.id, _MONDAY, "CAP", max_patients=3)
    assert updated.kind == "CAP"
    assert updated.max_patients == 3
    assert updated.start_time is None


def test_update_availability_window(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    row = AvailabilityController.add(doc, 0, time(9, 0), time(10, 0), 30)
    updated = AvailabilityController.update(doc, row.id, 2, time(14, 0), time(16, 0), 20)
    assert updated.weekday == 2
    assert updated.start_time == time(14, 0)
    assert updated.slot_minutes == 20
