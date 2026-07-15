"""Timezone-correct slot generation + availability exceptions (SQLite).

Slot wall-clock times are interpreted in the doctor's clinic timezone
(``DoctorProfile.timezone``) and returned as UTC-aware instants. A Casablanca
(UTC+1 in July) 09:00 window therefore yields an 08:00Z slot; a "UTC" clinic is
the control where 09:00 local == 09:00Z. ``AvailabilityException`` rows override
the recurring schedule per date (whole-day / timed BLOCK, one-off OPEN).

``DoctorProfile`` carries the PostGIS ``geopoint`` column stock SQLite cannot
compile, so the usual ``Geography -> TEXT`` + ``ST_GeogFromText`` passthrough
dialect shims are registered for the test engine (as in ``test_admin``).
"""

from datetime import UTC, date, datetime, time

import pytest
from geoalchemy2 import Geography
from geoalchemy2 import functions as geo_functions
from sehaty.db import (
    Appointment,
    Availability,
    AvailabilityException,
    AvailabilityExceptionKind,
    DoctorProfile,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.availability_exceptions import AvailabilityExceptionController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError
from sehaty.core.services.slots import available_slots, find_slot_end


@compiles(Geography, "sqlite")
def _geography_as_text_on_sqlite(element, compiler, **kw) -> str:  # noqa: ANN001
    return "TEXT"


@compiles(geo_functions.ST_GeogFromText, "sqlite")
def _geog_bind_passthrough_on_sqlite(element, compiler, **kw) -> str:  # noqa: ANN001
    return compiler.process(list(element.clauses)[0], **kw)


_TABLES = [
    User.__table__,
    DoctorProfile.__table__,
    Availability.__table__,
    AvailabilityException.__table__,
    Appointment.__table__,
]

# A fixed July weekday. Africa/Casablanca observes UTC+1 in July 2026.
_MONDAY = date(2026, 7, 13)
assert _MONDAY.weekday() == 0
_TUESDAY = date(2026, 7, 14)


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


def _seed_doctor(factory: sessionmaker[Session], *, tz: str, email: str = "doc@clinic.ma") -> int:
    local = email.split("@", 1)[0]
    with factory() as s:
        user = User(email=email, role=UserRole.DOCTOR, is_active=True)
        s.add(user)
        s.flush()
        s.add(
            DoctorProfile(
                user_id=user.id,
                full_name="Dr Test",
                slug=f"dr-{local}",
                license_no=f"L-{local}",
                timezone=tz,
            )
        )
        s.commit()
        return user.id


def _add_window(
    factory: sessionmaker[Session],
    doctor_id: int,
    *,
    weekday: int = 0,
    start: time = time(9, 0),
    end: time = time(12, 0),
    slot_minutes: int = 30,
) -> None:
    with factory() as s:
        s.add(
            Availability(
                doctor_id=doctor_id,
                weekday=weekday,
                start_time=start,
                end_time=end,
                slot_minutes=slot_minutes,
            )
        )
        s.commit()


# --------------------------------------------------------------------------- #
# Timezone-correct generation
# --------------------------------------------------------------------------- #


def test_casablanca_window_shifts_to_utc(db: sessionmaker[Session]) -> None:
    """09:00-12:00 Casablanca (UTC+1 in July) -> UTC starts from 08:00Z."""
    doc = _seed_doctor(db, tz="Africa/Casablanca")
    _add_window(db, doc)  # Monday 09:00-12:00, 30-min slots
    with db() as s:
        slots = available_slots(s, doc, _MONDAY, _MONDAY)

    assert len(slots) == 6
    starts = [s0 for s0, _ in slots]
    assert starts[0] == datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
    assert slots[0][1] == datetime(2026, 7, 13, 8, 30, tzinfo=UTC)
    assert starts == [
        datetime(2026, 7, 13, 8, 0, tzinfo=UTC),
        datetime(2026, 7, 13, 8, 30, tzinfo=UTC),
        datetime(2026, 7, 13, 9, 0, tzinfo=UTC),
        datetime(2026, 7, 13, 9, 30, tzinfo=UTC),
        datetime(2026, 7, 13, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 13, 10, 30, tzinfo=UTC),
    ]


def test_utc_clinic_is_identity_control(db: sessionmaker[Session]) -> None:
    """A UTC clinic: 09:00 local == 09:00Z (the control)."""
    doc = _seed_doctor(db, tz="UTC")
    _add_window(db, doc)
    with db() as s:
        slots = available_slots(s, doc, _MONDAY, _MONDAY)

    assert slots[0][0] == datetime(2026, 7, 13, 9, 0, tzinfo=UTC)
    assert slots[-1][0] == datetime(2026, 7, 13, 11, 30, tzinfo=UTC)


def test_missing_profile_defaults_to_casablanca(db: sessionmaker[Session]) -> None:
    """A doctor with no profile row falls back to Africa/Casablanca."""
    with db() as s:
        user = User(email="noprofile@clinic.ma", role=UserRole.DOCTOR, is_active=True)
        s.add(user)
        s.commit()
        doc = user.id
    _add_window(db, doc)
    with db() as s:
        slots = available_slots(s, doc, _MONDAY, _MONDAY)
    assert slots[0][0] == datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Exceptions: BLOCK / OPEN
# --------------------------------------------------------------------------- #


def test_whole_day_block_removes_all_slots(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, tz="Africa/Casablanca")
    _add_window(db, doc)
    AvailabilityExceptionController.add(
        doc, _MONDAY, AvailabilityExceptionKind.BLOCK, reason="holiday"
    )
    with db() as s:
        assert available_slots(s, doc, _MONDAY, _MONDAY) == []


def test_timed_block_removes_only_overlapping_local_slots(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, tz="Africa/Casablanca")
    _add_window(db, doc)  # local 09:00,09:30,10:00,10:30,11:00,11:30
    # Block 09:30-10:30 local -> removes the 09:30 and 10:00 local slots.
    AvailabilityExceptionController.add(
        doc, _MONDAY, AvailabilityExceptionKind.BLOCK, time(9, 30), time(10, 30)
    )
    with db() as s:
        slots = available_slots(s, doc, _MONDAY, _MONDAY)

    starts = {s0 for s0, _ in slots}
    assert len(slots) == 4
    # Removed local 09:30 (08:30Z) and 10:00 (09:00Z).
    assert datetime(2026, 7, 13, 8, 30, tzinfo=UTC) not in starts
    assert datetime(2026, 7, 13, 9, 0, tzinfo=UTC) not in starts
    # Kept local 09:00 (08:00Z) and 10:30 (09:30Z), the block-boundary slots.
    assert datetime(2026, 7, 13, 8, 0, tzinfo=UTC) in starts
    assert datetime(2026, 7, 13, 9, 30, tzinfo=UTC) in starts


def test_open_adds_extra_slots_on_specific_date(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, tz="Africa/Casablanca")
    _add_window(db, doc)  # recurring only on Monday
    # OPEN a one-off Tuesday 14:00-16:00 Casablanca, 30-min -> 4 slots @ 13:00Z..
    AvailabilityExceptionController.add(
        doc,
        _TUESDAY,
        AvailabilityExceptionKind.OPEN,
        time(14, 0),
        time(16, 0),
        slot_minutes=30,
    )
    with db() as s:
        slots = available_slots(s, doc, _TUESDAY, _TUESDAY)

    starts = [s0 for s0, _ in slots]
    assert starts == [
        datetime(2026, 7, 14, 13, 0, tzinfo=UTC),
        datetime(2026, 7, 14, 13, 30, tzinfo=UTC),
        datetime(2026, 7, 14, 14, 0, tzinfo=UTC),
        datetime(2026, 7, 14, 14, 30, tzinfo=UTC),
    ]


def test_open_adds_to_recurring_on_same_day(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, tz="UTC")
    _add_window(db, doc)  # Monday 09:00-12:00 UTC -> 6 slots
    AvailabilityExceptionController.add(
        doc,
        _MONDAY,
        AvailabilityExceptionKind.OPEN,
        time(14, 0),
        time(15, 0),
        slot_minutes=30,
    )
    with db() as s:
        slots = available_slots(s, doc, _MONDAY, _MONDAY)
    assert len(slots) == 8  # 6 recurring + 2 one-off
    assert datetime(2026, 7, 13, 14, 0, tzinfo=UTC) in {s0 for s0, _ in slots}


# --------------------------------------------------------------------------- #
# find_slot_end
# --------------------------------------------------------------------------- #


def test_find_slot_end_validates_real_slot(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, tz="Africa/Casablanca")
    _add_window(db, doc)
    with db() as s:
        # Real slot: local 09:00 == 08:00Z, ends 08:30Z.
        end = find_slot_end(s, doc, datetime(2026, 7, 13, 8, 0, tzinfo=UTC))
        assert end == datetime(2026, 7, 13, 8, 30, tzinfo=UTC)
        # Bogus: 09:00Z would be local 10:00 (a real slot) — but 08:15Z is not
        # on any boundary.
        assert find_slot_end(s, doc, datetime(2026, 7, 13, 8, 15, tzinfo=UTC)) is None
        # Outside the window entirely.
        assert find_slot_end(s, doc, datetime(2026, 7, 13, 20, 0, tzinfo=UTC)) is None


# --------------------------------------------------------------------------- #
# Exception controller: add / list / delete + validation + ownership
# --------------------------------------------------------------------------- #


def test_exception_add_and_list_ordered(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, tz="UTC")
    AvailabilityExceptionController.add(
        doc, _TUESDAY, AvailabilityExceptionKind.BLOCK, reason="off"
    )
    row = AvailabilityExceptionController.add(
        doc, _MONDAY, "OPEN", time(9, 0), time(10, 0), slot_minutes=30
    )
    assert row.date == _MONDAY
    assert row.kind == "OPEN"
    assert row.slot_minutes == 30

    rows = AvailabilityExceptionController.list(doc)
    assert [r.date for r in rows] == [_MONDAY, _TUESDAY]  # date-ordered


def test_exception_list_date_filter(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, tz="UTC")
    AvailabilityExceptionController.add(doc, _MONDAY, "BLOCK")
    AvailabilityExceptionController.add(doc, _TUESDAY, "BLOCK")
    rows = AvailabilityExceptionController.list(doc, date_from=_TUESDAY)
    assert [r.date for r in rows] == [_TUESDAY]


def test_exception_add_rejects_bad_kind(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, tz="UTC")
    with pytest.raises(SehatyValidationError):
        AvailabilityExceptionController.add(doc, _MONDAY, "MAYBE")


def test_exception_open_requires_window_and_slot(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, tz="UTC")
    with pytest.raises(SehatyValidationError):
        AvailabilityExceptionController.add(doc, _MONDAY, "OPEN")  # no window
    with pytest.raises(SehatyValidationError):
        AvailabilityExceptionController.add(
            doc, _MONDAY, "OPEN", time(10, 0), time(9, 0), slot_minutes=30
        )  # start >= end
    with pytest.raises(SehatyValidationError):
        AvailabilityExceptionController.add(
            doc, _MONDAY, "OPEN", time(9, 0), time(10, 0), slot_minutes=0
        )  # non-positive slot


def test_exception_timed_block_requires_ordered_window(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, tz="UTC")
    with pytest.raises(SehatyValidationError):
        AvailabilityExceptionController.add(doc, _MONDAY, "BLOCK", time(11, 0), time(10, 0))
    # A partial timed BLOCK (only one bound) is rejected.
    with pytest.raises(SehatyValidationError):
        AvailabilityExceptionController.add(doc, _MONDAY, "BLOCK", time(11, 0), None)


def test_exception_delete_and_ownership(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, tz="UTC")
    other = _seed_doctor(db, tz="UTC", email="other@clinic.ma")
    row = AvailabilityExceptionController.add(doc, _MONDAY, "BLOCK")

    # A different doctor cannot delete it.
    with pytest.raises(SehatyNotFoundError):
        AvailabilityExceptionController.delete(other, row.id)
    assert len(AvailabilityExceptionController.list(doc)) == 1

    AvailabilityExceptionController.delete(doc, row.id)
    assert AvailabilityExceptionController.list(doc) == []

    # Deleting a missing row raises.
    with pytest.raises(SehatyNotFoundError):
        AvailabilityExceptionController.delete(doc, 424242)
