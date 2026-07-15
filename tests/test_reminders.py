"""Reminder core tests on an in-memory SQLite engine.

Covers ``AppointmentController.run_reminders``: an imminent CONFIRMED
appointment is reminded exactly once (marker + patient notification), a second
run is a no-op (dedupe via ``reminder_sent_at``), and appointments outside the
window — too far ahead, wrong status, or already past — are left alone.

Appointments are inserted directly (no ``book`` / availability) so each test
controls ``start_at`` and ``status`` freely; no doctor ``DoctorProfile`` is
seeded, so no PostGIS geo shim is needed and the reminder message falls back to
the "your doctor" wording.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    Notification,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.appointments import AppointmentController
from sehaty.core.controllers.notifications import NotificationController
from sehaty.core.db import session as session_mod

_TABLES = [
    User.__table__,
    Appointment.__table__,
    Notification.__table__,
]

# A fixed "now" anchor; run_reminders is always called with now=_NOW so the
# window is deterministic regardless of the wall clock.
_NOW = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


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


def _seed_pair(factory: sessionmaker[Session]) -> tuple[int, int]:
    """Seed one doctor and one patient; return (doctor_id, patient_id)."""
    doc = _seed_user(factory, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(factory, email="pat@clinic.ma", role=UserRole.PATIENT)
    return doc, pat


def _seed_appt(
    factory: sessionmaker[Session],
    *,
    doctor_id: int,
    patient_id: int,
    start_at: datetime,
    status: AppointmentStatus,
) -> int:
    with factory() as s:
        appt = Appointment(
            patient_id=patient_id,
            doctor_id=doctor_id,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=status,
        )
        s.add(appt)
        s.commit()
        return appt.id


def _reminder_notifs(patient_id: int) -> list[Notification]:
    return [
        n for n in NotificationController.list_for(patient_id) if n.kind == "appointment_reminder"
    ]


def test_confirmed_within_window_is_reminded_once(db: sessionmaker[Session]) -> None:
    doc, pat = _seed_pair(db)
    appt_id = _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW + timedelta(hours=12),
        status=AppointmentStatus.CONFIRMED,
    )

    assert AppointmentController.run_reminders(now=_NOW) == 1

    notifs = _reminder_notifs(pat)
    assert len(notifs) == 1
    assert notifs[0].entity == "appointment"
    assert notifs[0].entity_id == appt_id
    with db() as s:
        appt = s.get(Appointment, appt_id)
    assert appt.reminder_sent_at is not None


def test_second_run_does_not_re_notify(db: sessionmaker[Session]) -> None:
    doc, pat = _seed_pair(db)
    _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW + timedelta(hours=12),
        status=AppointmentStatus.CONFIRMED,
    )

    assert AppointmentController.run_reminders(now=_NOW) == 1
    # Dedupe: reminder_sent_at is now set, so the second run finds nothing.
    assert AppointmentController.run_reminders(now=_NOW) == 0
    assert len(_reminder_notifs(pat)) == 1


def test_appointment_outside_window_not_reminded(db: sessionmaker[Session]) -> None:
    doc, pat = _seed_pair(db)
    _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW + timedelta(days=3),
        status=AppointmentStatus.CONFIRMED,
    )

    assert AppointmentController.run_reminders(now=_NOW) == 0
    assert _reminder_notifs(pat) == []


def test_requested_appointment_not_reminded(db: sessionmaker[Session]) -> None:
    doc, pat = _seed_pair(db)
    _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW + timedelta(hours=12),
        status=AppointmentStatus.REQUESTED,
    )

    assert AppointmentController.run_reminders(now=_NOW) == 0
    assert _reminder_notifs(pat) == []


def test_past_appointment_not_reminded(db: sessionmaker[Session]) -> None:
    doc, pat = _seed_pair(db)
    _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW - timedelta(hours=1),
        status=AppointmentStatus.CONFIRMED,
    )

    assert AppointmentController.run_reminders(now=_NOW) == 0
    assert _reminder_notifs(pat) == []
