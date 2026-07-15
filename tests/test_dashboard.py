"""Doctor dashboard-stats tests on an in-memory SQLite engine.

Covers ``DashboardController.doctor_stats``: today's totals (UTC calendar date,
CONFIRMED subset), the ``to_confirm`` upcoming-REQUESTED count, the 7-day
upcoming window (excluding a further-out and a past appointment), the register
size, the soonest ``next_appointment`` with its resolved patient name, and doctor
scoping. Only the tables this feature touches (users, clinic_patients,
appointments) are created — none carry the PostGIS ``geopoint`` column, so the
column-only projections run on SQLite.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    ClinicPatient,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.dashboard import DashboardController
from sehaty.core.db import session as session_mod

_TABLES = [
    User.__table__,
    ClinicPatient.__table__,
    Appointment.__table__,
]

# Fixed "now" so today's window and the 7-day window are deterministic. Mid-day
# so "today" appointments a few hours either side stay on the same UTC date.
_NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


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


def _seed_user(
    factory: sessionmaker[Session],
    *,
    email: str,
    role: UserRole,
    phone: str | None = None,
) -> int:
    with factory() as s:
        user = User(email=email, phone=phone, role=role, is_active=True)
        s.add(user)
        s.commit()
        return user.id


def _seed_register(
    factory: sessionmaker[Session],
    *,
    doctor_id: int,
    user_id: int | None = None,
    full_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> int:
    with factory() as s:
        cp = ClinicPatient(
            doctor_id=doctor_id,
            user_id=user_id,
            full_name=full_name,
            email=email,
            phone=phone,
            created_by=doctor_id,
        )
        s.add(cp)
        s.commit()
        return cp.id


def _seed_appt(
    factory: sessionmaker[Session],
    *,
    doctor_id: int,
    patient_id: int,
    start_at: datetime,
    clinic_patient_id: int | None = None,
    status: AppointmentStatus = AppointmentStatus.REQUESTED,
) -> int:
    with factory() as s:
        appt = Appointment(
            patient_id=patient_id,
            doctor_id=doctor_id,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=status,
            clinic_patient_id=clinic_patient_id,
        )
        s.add(appt)
        s.commit()
        return appt.id


def test_doctor_stats_full_mix(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    cp = _seed_register(db, doctor_id=doc, user_id=pat, full_name="Amina Zahra")
    # A second register patient (no appointments) so patients_total = 2.
    _seed_register(db, doctor_id=doc, full_name="Walk In")

    # Two appointments today: one CONFIRMED (this afternoon), one REQUESTED (soonest).
    today_confirmed = _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW + timedelta(hours=3),
        clinic_patient_id=cp,
        status=AppointmentStatus.CONFIRMED,
    )
    today_requested = _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW + timedelta(hours=1),
        clinic_patient_id=cp,
        status=AppointmentStatus.REQUESTED,
    )
    # One REQUESTED tomorrow (upcoming, within 7d).
    tomorrow_requested = _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW + timedelta(days=1),
        clinic_patient_id=cp,
        status=AppointmentStatus.REQUESTED,
    )
    # One CONFIRMED in 10 days — outside the 7-day window.
    _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW + timedelta(days=10),
        clinic_patient_id=cp,
        status=AppointmentStatus.CONFIRMED,
    )
    # One past appointment (yesterday) — excluded everywhere upcoming.
    _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW - timedelta(days=1),
        clinic_patient_id=cp,
        status=AppointmentStatus.CONFIRMED,
    )

    stats = DashboardController.doctor_stats(doc, now=_NOW)

    # Today: the two appointments dated on today's UTC date; one is CONFIRMED.
    assert stats.today_total == 2
    assert stats.today_confirmed == 1
    # to_confirm: upcoming REQUESTED — today's requested + tomorrow's requested = 2.
    assert stats.to_confirm == 2
    # upcoming_7d: today's two + tomorrow's requested = 3 (10-days-out and past excluded).
    assert stats.upcoming_7d == 3
    assert stats.patients_total == 2
    # next_appointment: soonest upcoming = today's REQUESTED at now+1h.
    assert stats.next_appointment is not None
    # SQLite returns naive datetimes; compare the wall-clock instant tz-agnostically.
    expected = (_NOW + timedelta(hours=1)).replace(tzinfo=None)
    assert stats.next_appointment.start_at.replace(tzinfo=None) == expected
    assert stats.next_appointment.status == str(AppointmentStatus.REQUESTED)
    assert stats.next_appointment.patient_name == "Amina Zahra"
    # Sanity: the ids we expect are distinct and used above.
    assert {today_confirmed, today_requested, tomorrow_requested}


def test_next_appointment_none_when_nothing_upcoming(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    # Only a past appointment and a cancelled future one -> nothing upcoming.
    _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW - timedelta(days=2),
        status=AppointmentStatus.COMPLETED,
    )
    _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW + timedelta(days=1),
        status=AppointmentStatus.CANCELLED,
    )

    stats = DashboardController.doctor_stats(doc, now=_NOW)
    assert stats.today_total == 0
    assert stats.to_confirm == 0
    assert stats.upcoming_7d == 0
    assert stats.next_appointment is None


def test_next_appointment_name_falls_back_to_user_then_label(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(
        db, email="pat.contact@clinic.ma", phone="+212611111111", role=UserRole.PATIENT
    )
    # No register link -> name resolves via the booking User.
    _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW + timedelta(hours=2),
        clinic_patient_id=None,
        status=AppointmentStatus.CONFIRMED,
    )
    stats = DashboardController.doctor_stats(doc, now=_NOW)
    assert stats.next_appointment is not None
    assert stats.next_appointment.patient_name == "pat.contact@clinic.ma"


def test_doctor_stats_scoped_to_doctor(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    other = _seed_user(db, email="other@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    cp = _seed_register(db, doctor_id=doc, user_id=pat, full_name="Mine")
    _seed_register(db, doctor_id=other, user_id=pat, full_name="Theirs")

    _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW + timedelta(hours=1),
        clinic_patient_id=cp,
        status=AppointmentStatus.REQUESTED,
    )
    # The other doctor has a sooner appointment that must NOT leak into doc's stats.
    _seed_appt(
        db,
        doctor_id=other,
        patient_id=pat,
        start_at=_NOW + timedelta(minutes=10),
        status=AppointmentStatus.REQUESTED,
    )

    stats = DashboardController.doctor_stats(doc, now=_NOW)
    assert stats.today_total == 1
    assert stats.to_confirm == 1
    assert stats.upcoming_7d == 1
    # Register scoping: only doc's own register row is counted.
    assert stats.patients_total == 1
    assert stats.next_appointment is not None
    assert stats.next_appointment.patient_name == "Mine"


def test_now_defaults_and_naive_now_normalised(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_NOW + timedelta(hours=1),
        status=AppointmentStatus.REQUESTED,
    )
    # A naive now is treated as UTC — same result as the aware one.
    stats = DashboardController.doctor_stats(doc, now=_NOW.replace(tzinfo=None))
    assert stats.to_confirm == 1
    assert stats.next_appointment is not None
