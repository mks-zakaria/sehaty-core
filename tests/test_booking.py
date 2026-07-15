"""Booking core tests on an in-memory SQLite engine.

Covers doctor availability CRUD + ownership, slot generation from weekly
windows, exclusion of booked slots, double-booking conflict, and the
role-based appointment transition matrix. Only the tables this feature touches
(users, availabilities, appointments, audit_logs) are created — none of them
carry the PostGIS ``geopoint`` column, so no dialect shims are needed.
"""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    AuditLog,
    Availability,
    ClinicPatient,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.appointments import AppointmentController
from sehaty.core.controllers.availability import AvailabilityController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)
from sehaty.core.services.slots import available_slots, find_slot_end

_TABLES = [
    User.__table__,
    Availability.__table__,
    ClinicPatient.__table__,
    Appointment.__table__,
    AuditLog.__table__,
]

# A fixed Monday used as the anchor for weekday-aligned slot tests.
_MONDAY = date(2026, 8, 3)
assert _MONDAY.weekday() == 0


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


def _seed_user(factory: sessionmaker[Session], *, email: str, role: UserRole) -> int:
    with factory() as s:
        user = User(email=email, role=role, is_active=True)
        s.add(user)
        s.commit()
        return user.id


def _seed_doctor(factory: sessionmaker[Session], email: str = "doc@clinic.ma") -> int:
    return _seed_user(factory, email=email, role=UserRole.DOCTOR)


def _seed_patient(factory: sessionmaker[Session], email: str = "pat@clinic.ma") -> int:
    return _seed_user(factory, email=email, role=UserRole.PATIENT)


# --------------------------------------------------------------------------- #
# Availability CRUD + ownership
# --------------------------------------------------------------------------- #


def test_add_and_list_availability(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    AvailabilityController.add(doc, 2, time(9, 0), time(12, 0), 30)
    AvailabilityController.add(doc, 0, time(14, 0), time(16, 0), 20)

    rows = AvailabilityController.list(doc)
    # Ordered by weekday then start_time.
    assert [r.weekday for r in rows] == [0, 2]
    assert rows[0].start_time == time(14, 0)
    assert rows[0].slot_minutes == 20


def test_add_invalid_weekday_raises(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        AvailabilityController.add(doc, 7, time(9, 0), time(12, 0), 30)


def test_add_start_not_before_end_raises(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        AvailabilityController.add(doc, 1, time(12, 0), time(9, 0), 30)


def test_delete_availability(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    avail = AvailabilityController.add(doc, 1, time(9, 0), time(10, 0), 30)
    AvailabilityController.delete(doc, avail.id)
    assert AvailabilityController.list(doc) == []


def test_delete_not_owned_raises(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    other = _seed_doctor(db, email="other@clinic.ma")
    avail = AvailabilityController.add(doc, 1, time(9, 0), time(10, 0), 30)
    with pytest.raises(SehatyNotFoundError):
        AvailabilityController.delete(other, avail.id)
    # Still there for the real owner.
    assert len(AvailabilityController.list(doc)) == 1


def test_delete_missing_raises(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyNotFoundError):
        AvailabilityController.delete(doc, 424242)


# --------------------------------------------------------------------------- #
# Slot generation
# --------------------------------------------------------------------------- #


def test_slot_generation_count(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    # Monday 09:00-12:00, 30-min slots -> 6 slots.
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)
    with db() as s:
        slots = available_slots(s, doc, _MONDAY, _MONDAY)
    assert len(slots) == 6
    assert slots[0][0] == datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    assert slots[0][1] == datetime(2026, 8, 3, 9, 30, tzinfo=UTC)
    assert slots[-1][0] == datetime(2026, 8, 3, 11, 30, tzinfo=UTC)


def test_slot_generation_only_matching_weekday(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)
    tuesday = _MONDAY + timedelta(days=1)
    with db() as s:
        assert available_slots(s, doc, tuesday, tuesday) == []


def test_booked_slot_is_excluded(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)

    start = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    AppointmentController.book(pat, doc, start, reason="checkup")

    with db() as s:
        slots = available_slots(s, doc, _MONDAY, _MONDAY)
    assert len(slots) == 5
    assert all(sl[0] != start for sl in slots)


def test_cancelled_slot_frees_up(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)
    start = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    appt = AppointmentController.book(pat, doc, start)
    AppointmentController.transition(pat, UserRole.PATIENT, appt.id, AppointmentStatus.CANCELLED)
    with db() as s:
        slots = available_slots(s, doc, _MONDAY, _MONDAY)
    assert len(slots) == 6


def test_find_slot_end(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)
    with db() as s:
        end = find_slot_end(s, doc, datetime(2026, 8, 3, 9, 0, tzinfo=UTC))
        assert end == datetime(2026, 8, 3, 9, 30, tzinfo=UTC)
        # A start that is not on a slot boundary is rejected.
        assert find_slot_end(s, doc, datetime(2026, 8, 3, 9, 15, tzinfo=UTC)) is None


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #


def test_book_creates_requested_and_audits(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)
    start = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)

    appt = AppointmentController.book(pat, doc, start, reason="fever")

    assert appt.status == AppointmentStatus.REQUESTED
    assert appt.end_at == datetime(2026, 8, 3, 9, 30, tzinfo=UTC)
    assert appt.reason == "fever"
    with db() as s:
        log = s.execute(select(AuditLog).where(AuditLog.entity_id == appt.id)).scalar_one()
        assert log.action == "BOOK"
        assert log.entity == "appointment"
        assert log.actor_user_id == pat


def test_book_outside_availability_conflicts(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)
    # 13:00 is outside the 09:00-12:00 window.
    with pytest.raises(SehatyConflictError):
        AppointmentController.book(pat, doc, datetime(2026, 8, 3, 13, 0, tzinfo=UTC))


def test_double_booking_conflicts(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    pat1 = _seed_patient(db)
    pat2 = _seed_patient(db, email="pat2@clinic.ma")
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)
    start = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)

    AppointmentController.book(pat1, doc, start)
    with pytest.raises(SehatyConflictError):
        AppointmentController.book(pat2, doc, start)


def test_list_for_scopes_by_role(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    other_pat = _seed_patient(db, email="other@clinic.ma")
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)
    AppointmentController.book(pat, doc, datetime(2026, 8, 3, 9, 0, tzinfo=UTC))
    AppointmentController.book(pat, doc, datetime(2026, 8, 3, 9, 30, tzinfo=UTC))

    assert len(AppointmentController.list_for(pat, UserRole.PATIENT)) == 2
    assert len(AppointmentController.list_for(other_pat, UserRole.PATIENT)) == 0
    doc_appts = AppointmentController.list_for(doc, UserRole.DOCTOR)
    assert len(doc_appts) == 2
    # Ordered by start_at.
    assert doc_appts[0].start_at < doc_appts[1].start_at


# --------------------------------------------------------------------------- #
# Transition matrix
# --------------------------------------------------------------------------- #


def _fresh_appointment(db: sessionmaker[Session]) -> tuple[int, int, int]:
    """Book one appointment; return (doctor_id, patient_id, appointment_id)."""
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)
    appt = AppointmentController.book(pat, doc, datetime(2026, 8, 3, 9, 0, tzinfo=UTC))
    return doc, pat, appt.id


def test_doctor_confirms_then_completes(db: sessionmaker[Session]) -> None:
    doc, _pat, appt_id = _fresh_appointment(db)
    a = AppointmentController.transition(doc, UserRole.DOCTOR, appt_id, AppointmentStatus.CONFIRMED)
    assert a.status == AppointmentStatus.CONFIRMED
    a = AppointmentController.transition(
        doc, UserRole.DOCTOR, appt_id, AppointmentStatus.COMPLETED, notes="all good"
    )
    assert a.status == AppointmentStatus.COMPLETED
    assert a.notes == "all good"


def test_doctor_no_show_from_confirmed(db: sessionmaker[Session]) -> None:
    doc, _pat, appt_id = _fresh_appointment(db)
    AppointmentController.transition(doc, UserRole.DOCTOR, appt_id, AppointmentStatus.CONFIRMED)
    a = AppointmentController.transition(doc, UserRole.DOCTOR, appt_id, AppointmentStatus.NO_SHOW)
    assert a.status == AppointmentStatus.NO_SHOW


def test_doctor_cannot_complete_requested(db: sessionmaker[Session]) -> None:
    doc, _pat, appt_id = _fresh_appointment(db)
    # COMPLETED is only reachable from CONFIRMED.
    with pytest.raises(SehatyConflictError):
        AppointmentController.transition(doc, UserRole.DOCTOR, appt_id, AppointmentStatus.COMPLETED)


def test_doctor_cannot_no_show_requested(db: sessionmaker[Session]) -> None:
    doc, _pat, appt_id = _fresh_appointment(db)
    with pytest.raises(SehatyConflictError):
        AppointmentController.transition(doc, UserRole.DOCTOR, appt_id, AppointmentStatus.NO_SHOW)


def test_patient_can_cancel(db: sessionmaker[Session]) -> None:
    _doc, pat, appt_id = _fresh_appointment(db)
    a = AppointmentController.transition(
        pat, UserRole.PATIENT, appt_id, AppointmentStatus.CANCELLED
    )
    assert a.status == AppointmentStatus.CANCELLED


def test_patient_cannot_confirm(db: sessionmaker[Session]) -> None:
    _doc, pat, appt_id = _fresh_appointment(db)
    with pytest.raises(SehatyConflictError):
        AppointmentController.transition(
            pat, UserRole.PATIENT, appt_id, AppointmentStatus.CONFIRMED
        )


def test_wrong_actor_forbidden(db: sessionmaker[Session]) -> None:
    _doc, _pat, appt_id = _fresh_appointment(db)
    stranger = _seed_patient(db, email="stranger@clinic.ma")
    with pytest.raises(SehatyForbiddenError):
        AppointmentController.transition(
            stranger, UserRole.PATIENT, appt_id, AppointmentStatus.CANCELLED
        )


def test_transition_writes_audit(db: sessionmaker[Session]) -> None:
    doc, _pat, appt_id = _fresh_appointment(db)
    AppointmentController.transition(doc, UserRole.DOCTOR, appt_id, AppointmentStatus.CONFIRMED)
    with db() as s:
        actions = set(
            s.execute(select(AuditLog.action).where(AuditLog.entity_id == appt_id)).scalars()
        )
    assert "BOOK" in actions
    assert "APPT_CONFIRMED" in actions
