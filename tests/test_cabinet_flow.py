"""Cabinet + consultation flow on an in-memory SQLite engine.

Covers the secretary/cabinet feature end to end: create a cabinet, open a session
worked by a *substitute* doctor, the secretary checks a patient in (which notifies
the acting doctor), the doctor's waiting queue, and the doctor starting and
completing the consultation (recording the encounter). Only the tables this
feature touches are created — none carry the PostGIS ``geopoint`` column.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    AuditLog,
    Cabinet,
    CabinetSession,
    ClinicPatient,
    Notification,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.appointments import AppointmentController
from sehaty.core.controllers.cabinet import CabinetController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyConflictError, SehatyForbiddenError

_TABLES = [
    User.__table__,
    ClinicPatient.__table__,
    Cabinet.__table__,
    CabinetSession.__table__,
    Appointment.__table__,
    AuditLog.__table__,
    Notification.__table__,
]

_SLOT = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


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


def _seed_doctor(factory, email: str) -> int:
    with factory() as s:
        u = User(email=email, role=UserRole.DOCTOR, is_active=True)
        s.add(u)
        s.commit()
        return u.id


def _seed_appt(factory, doctor_id: int, register_id: int, status: AppointmentStatus) -> int:
    with factory() as s:
        appt = Appointment(
            patient_id=999,
            doctor_id=doctor_id,
            clinic_patient_id=register_id,
            start_at=_SLOT,
            end_at=_SLOT + timedelta(minutes=30),
            status=status,
        )
        s.add(appt)
        s.commit()
        return appt.id


def _seed_register(factory, doctor_id: int) -> int:
    with factory() as s:
        cp = ClinicPatient(
            doctor_id=doctor_id,
            full_name="Salma B.",
            phone="+212600000001",
            sex="F",
            birth_year=1990,
        )
        s.add(cp)
        s.commit()
        return cp.id


def test_full_consultation_flow_with_substitute(db):
    owner = _seed_doctor(db, "owner@clinic.ma")
    substitute = _seed_doctor(db, "friend@clinic.ma")  # covers for the owner
    reg = _seed_register(db, owner)
    appt = _seed_appt(db, owner, reg, AppointmentStatus.CONFIRMED)

    cab = CabinetController.create(owner, "Cabinet Centre-Ville", address="12 Rue X")
    assert cab.owner_doctor_id == owner and cab.is_active

    sess = CabinetController.open_session(cab.id, substitute)
    assert sess.is_open and sess.acting_doctor_id == substitute  # substitute is online

    # Secretary checks the patient in -> CHECKED_IN, linked to the session.
    row = AppointmentController.check_in(appt, sess.id)
    assert row.status == AppointmentStatus.CHECKED_IN

    # The ACTING doctor (substitute), not the owner, was notified.
    with db() as s:
        kinds_sub = [
            n.kind for n in s.execute(
                select(Notification).where(Notification.user_id == substitute)
            ).scalars()
        ]
        kinds_owner = [
            n.kind for n in s.execute(
                select(Notification).where(Notification.user_id == owner)
            ).scalars()
        ]
    assert "next_patient" in kinds_sub
    assert "next_patient" not in kinds_owner

    # The doctor's queue shows the patient with their profile.
    queue = AppointmentController.waiting_queue(substitute)
    assert len(queue) == 1
    assert queue[0].appointment_id == appt
    assert queue[0].patient_name == "Salma B."
    assert queue[0].sex == "F" and queue[0].birth_year == 1990

    # The owner (not the acting doctor) may not start this consultation.
    with pytest.raises(SehatyForbiddenError):
        AppointmentController.start_consultation(appt, owner)

    started = AppointmentController.start_consultation(appt, substitute)
    assert started.status == AppointmentStatus.IN_PROGRESS
    assert started.consultation_started_at is not None

    done = AppointmentController.complete_consultation(
        appt,
        substitute,
        chief_complaint="fever, cough",
        symptoms={"items": ["fever", "cough"]},
        vitals={"temp_c": 38.5, "hr": 96},
        exam_notes="Clear chest.",
    )
    assert done.status == AppointmentStatus.COMPLETED
    assert done.consultation_ended_at is not None
    assert done.chief_complaint == "fever, cough"
    assert done.vitals["temp_c"] == 38.5
    # Queue is empty once the patient is being/has been seen.
    assert AppointmentController.waiting_queue(substitute) == []


def test_active_session_for_owner(db):
    owner = _seed_doctor(db, "owner2@clinic.ma")
    substitute = _seed_doctor(db, "sub@clinic.ma")
    cab = CabinetController.create(owner, "C-owner")

    # Nobody online yet.
    assert CabinetController.active_session_for_owner(owner) is None

    # A substitute opens the shift; the owner's secretary can still discover it.
    sess = CabinetController.open_session(cab.id, substitute)
    found = CabinetController.active_session_for_owner(owner)
    assert found is not None
    assert found.id == sess.id
    assert found.acting_doctor_id == substitute  # substitute covering the owner

    # Closing it goes back to "nobody online".
    CabinetController.close_session(sess.id)
    assert CabinetController.active_session_for_owner(owner) is None


def test_only_one_open_session_per_cabinet(db):
    owner = _seed_doctor(db, "o@clinic.ma")
    cab = CabinetController.create(owner, "C1")
    CabinetController.open_session(cab.id, owner)
    with pytest.raises(SehatyConflictError):
        CabinetController.open_session(cab.id, owner)


def test_cannot_start_before_check_in(db):
    owner = _seed_doctor(db, "o2@clinic.ma")
    reg = _seed_register(db, owner)
    appt = _seed_appt(db, owner, reg, AppointmentStatus.CONFIRMED)
    # Never checked in -> no cabinet session linked.
    with pytest.raises(SehatyConflictError):
        AppointmentController.start_consultation(appt, owner)


def test_check_in_rejects_closed_session(db):
    owner = _seed_doctor(db, "o3@clinic.ma")
    reg = _seed_register(db, owner)
    appt = _seed_appt(db, owner, reg, AppointmentStatus.CONFIRMED)
    cab = CabinetController.create(owner, "C3")
    sess = CabinetController.open_session(cab.id, owner)
    CabinetController.close_session(sess.id)
    with pytest.raises(SehatyConflictError):
        AppointmentController.check_in(appt, sess.id)
