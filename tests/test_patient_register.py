"""Patient-register core tests on an in-memory SQLite engine.

Covers auto-linking a booking to the doctor's ``clinic_patients`` register,
the live-aggregated grid (``list_patients`` counts / last-visit / next-appt,
search, every sort order, doctor scoping), the detail view with newest-first
visit history, and the manual add / update flows. Only the tables this feature
touches (users, availabilities, clinic_patients, appointments, audit_logs) are
created — none carry the PostGIS ``geopoint`` column, so no dialect shim is
needed.
"""

from datetime import UTC, datetime, time, timedelta

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
from sehaty.core.controllers.patients import PatientRegisterController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError

_TABLES = [
    User.__table__,
    Availability.__table__,
    ClinicPatient.__table__,
    Appointment.__table__,
    AuditLog.__table__,
]

# A fixed Monday anchor for weekday-aligned booking slots.
_MONDAY = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
assert _MONDAY.weekday() == 0
_PAST = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
_FUTURE = datetime(2026, 12, 7, 9, 0, tzinfo=UTC)


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


def _seed_user(factory: sessionmaker[Session], *, email: str, role: UserRole, phone=None) -> int:
    with factory() as s:
        user = User(email=email, role=role, is_active=True, phone=phone)
        s.add(user)
        s.commit()
        return user.id


def _seed_doctor(factory: sessionmaker[Session], email: str = "doc@clinic.ma") -> int:
    return _seed_user(factory, email=email, role=UserRole.DOCTOR)


def _seed_patient(factory, email="pat@clinic.ma", phone=None) -> int:
    return _seed_user(factory, email=email, role=UserRole.PATIENT, phone=phone)


def _seed_register(
    factory: sessionmaker[Session], doctor_id: int, *, full_name=None, phone=None, email=None
) -> int:
    """Insert a ClinicPatient row directly and return its id."""
    with factory() as s:
        cp = ClinicPatient(doctor_id=doctor_id, full_name=full_name, phone=phone, email=email)
        s.add(cp)
        s.commit()
        return cp.id


def _seed_appt(
    factory: sessionmaker[Session],
    doctor_id: int,
    clinic_patient_id: int,
    start_at: datetime,
    status: AppointmentStatus,
    *,
    patient_id: int = 999,
    reason=None,
    notes=None,
) -> int:
    with factory() as s:
        appt = Appointment(
            patient_id=patient_id,
            doctor_id=doctor_id,
            clinic_patient_id=clinic_patient_id,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=status,
            reason=reason,
            notes=notes,
        )
        s.add(appt)
        s.commit()
        return appt.id


# --------------------------------------------------------------------------- #
# Auto-link on booking
# --------------------------------------------------------------------------- #


def test_booking_auto_creates_and_links_register_row(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db, phone="+212600000001")
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)

    appt1 = AppointmentController.book(pat, doc, _MONDAY, reason="checkup")
    appt2 = AppointmentController.book(pat, doc, _MONDAY + timedelta(minutes=30))

    # Both bookings link to the SAME register row (one row per doctor+patient).
    assert appt1.clinic_patient_id is not None
    assert appt1.clinic_patient_id == appt2.clinic_patient_id

    with db() as s:
        rows = (
            s.execute(select(ClinicPatient).where(ClinicPatient.doctor_id == doc)).scalars().all()
        )
    assert len(rows) == 1
    cp = rows[0]
    assert cp.user_id == pat
    assert cp.phone == "+212600000001"  # backfilled from the patient User
    assert cp.email == "pat@clinic.ma"
    assert cp.full_name is None  # patients carry no stored name


def test_booking_reuses_register_across_doctors(db: sessionmaker[Session]) -> None:
    doc1 = _seed_doctor(db, email="d1@clinic.ma")
    doc2 = _seed_doctor(db, email="d2@clinic.ma")
    pat = _seed_patient(db)
    AvailabilityController.add(doc1, 0, time(9, 0), time(12, 0), 30)
    AvailabilityController.add(doc2, 0, time(9, 0), time(12, 0), 30)

    a1 = AppointmentController.book(pat, doc1, _MONDAY)
    a2 = AppointmentController.book(pat, doc2, _MONDAY)
    # Different doctors -> different register rows.
    assert a1.clinic_patient_id != a2.clinic_patient_id


# --------------------------------------------------------------------------- #
# list_patients aggregation
# --------------------------------------------------------------------------- #


def test_list_patients_aggregates(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc, full_name="Amina", phone="0600")
    _seed_appt(db, doc, cp, _PAST, AppointmentStatus.COMPLETED)
    _seed_appt(db, doc, cp, _PAST - timedelta(days=10), AppointmentStatus.COMPLETED)
    _seed_appt(db, doc, cp, _PAST + timedelta(days=1), AppointmentStatus.NO_SHOW)
    _seed_appt(db, doc, cp, _FUTURE, AppointmentStatus.CONFIRMED)
    _seed_appt(db, doc, cp, _FUTURE + timedelta(days=2), AppointmentStatus.REQUESTED)

    [row] = PatientRegisterController.list_patients(doc)
    assert row.total_visits == 5
    assert row.completed_visits == 2
    assert row.no_show_count == 1
    # SQLite drops tzinfo on the round-trip (Postgres timezone=True keeps it),
    # so compare the naive wall-clock value.
    assert row.last_visit_at.replace(tzinfo=None) == _PAST.replace(tzinfo=None)
    assert row.next_appointment_at.replace(tzinfo=None) == _FUTURE.replace(tzinfo=None)


def test_list_patients_no_appointments(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    _seed_register(db, doc, full_name="Walkin")
    [row] = PatientRegisterController.list_patients(doc)
    assert row.total_visits == 0
    assert row.completed_visits == 0
    assert row.no_show_count == 0
    assert row.last_visit_at is None
    assert row.next_appointment_at is None


def test_list_patients_search(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    _seed_register(db, doc, full_name="Amina Alaoui", phone="0611")
    _seed_register(db, doc, full_name="Youssef Bennani", phone="0622", email="y@x.ma")

    assert [r.full_name for r in PatientRegisterController.list_patients(doc, search="amina")] == [
        "Amina Alaoui"
    ]
    # phone match
    assert len(PatientRegisterController.list_patients(doc, search="0622")) == 1
    # email match (case-insensitive)
    assert len(PatientRegisterController.list_patients(doc, search="Y@X")) == 1
    assert PatientRegisterController.list_patients(doc, search="zzz") == []


def test_list_patients_sorts(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    a = _seed_register(db, doc, full_name="Amina")
    b = _seed_register(db, doc, full_name="Bilal")
    c = _seed_register(db, doc, full_name=None)  # NULL name -> last on name sort

    # a: 1 completed (recent), 3 total; b: 1 completed (older), 1 total; b upcoming.
    _seed_appt(db, doc, a, _PAST, AppointmentStatus.COMPLETED)
    _seed_appt(db, doc, a, _PAST, AppointmentStatus.NO_SHOW)
    _seed_appt(db, doc, a, _PAST, AppointmentStatus.CANCELLED)
    _seed_appt(db, doc, b, _PAST - timedelta(days=30), AppointmentStatus.COMPLETED)
    _seed_appt(db, doc, b, _FUTURE, AppointmentStatus.CONFIRMED)

    names = lambda rows: [r.id for r in rows]  # noqa: E731

    # name: full_name asc, NULLs last
    assert names(PatientRegisterController.list_patients(doc, sort="name")) == [a, b, c]
    # visits: total desc (a=3, b=2, c=0)
    assert names(PatientRegisterController.list_patients(doc, sort="visits")) == [a, b, c]
    # recent: last_visit_at desc, NULLs last (a more recent than b, c none)
    assert names(PatientRegisterController.list_patients(doc, sort="recent")) == [a, b, c]
    # upcoming: next_appointment_at asc, NULLs last (only b has an upcoming)
    assert names(PatientRegisterController.list_patients(doc, sort="upcoming"))[0] == b


def test_list_patients_invalid_sort_raises(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        PatientRegisterController.list_patients(doc, sort="bogus")


def test_list_patients_doctor_scoping(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    _seed_register(db, doc, full_name="Mine")
    _seed_register(db, other, full_name="Theirs")

    mine = PatientRegisterController.list_patients(doc)
    assert [r.full_name for r in mine] == ["Mine"]


# --------------------------------------------------------------------------- #
# get_patient
# --------------------------------------------------------------------------- #


def test_get_patient_history_newest_first(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc, full_name="Amina")
    _seed_appt(db, doc, cp, _PAST, AppointmentStatus.COMPLETED, reason="old")
    _seed_appt(db, doc, cp, _FUTURE, AppointmentStatus.CONFIRMED, reason="new")

    detail = PatientRegisterController.get_patient(doc, cp)
    assert detail.full_name == "Amina"
    assert detail.total_visits == 2
    assert [v.reason for v in detail.history] == ["new", "old"]  # newest-first
    assert detail.history[0].start_at.replace(tzinfo=None) == _FUTURE.replace(tzinfo=None)


def test_get_patient_no_show_count_live_matches_list(db: sessionmaker[Session]) -> None:
    """Detail no_show_count is computed live and equals what the list reports.

    The stored ClinicPatient.no_show_count column is never incremented (default
    0), so detail must derive the count from the patient's appointments — the
    same source list_patients aggregates — and the two must agree.
    """
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc, full_name="Amina")
    _seed_appt(db, doc, cp, _PAST, AppointmentStatus.COMPLETED)
    _seed_appt(db, doc, cp, _PAST + timedelta(days=1), AppointmentStatus.NO_SHOW)

    # The stored column is stale (always 0), proving the value is live-derived.
    with db() as s:
        assert s.get(ClinicPatient, cp).no_show_count == 0

    detail = PatientRegisterController.get_patient(doc, cp)
    [row] = PatientRegisterController.list_patients(doc)
    assert detail.no_show_count == 1
    assert detail.no_show_count == row.no_show_count  # detail == list


def test_get_patient_no_show_count_two(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc, full_name="Bilal")
    _seed_appt(db, doc, cp, _PAST, AppointmentStatus.NO_SHOW)
    _seed_appt(db, doc, cp, _PAST + timedelta(days=1), AppointmentStatus.NO_SHOW)
    _seed_appt(db, doc, cp, _FUTURE, AppointmentStatus.CONFIRMED)

    detail = PatientRegisterController.get_patient(doc, cp)
    [row] = PatientRegisterController.list_patients(doc)
    assert detail.no_show_count == 2
    assert detail.no_show_count == row.no_show_count


def test_get_patient_no_show_count_zero(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc, full_name="Chaimae")
    _seed_appt(db, doc, cp, _PAST, AppointmentStatus.COMPLETED)

    detail = PatientRegisterController.get_patient(doc, cp)
    [row] = PatientRegisterController.list_patients(doc)
    assert detail.no_show_count == 0
    assert detail.no_show_count == row.no_show_count


def test_update_patient_no_show_count_live(db: sessionmaker[Session]) -> None:
    """The detail returned by update_patient also carries the live count."""
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc, full_name="Dcounia")
    _seed_appt(db, doc, cp, _PAST, AppointmentStatus.NO_SHOW)

    detail = PatientRegisterController.update_patient(doc, cp, notes="flaky")
    [row] = PatientRegisterController.list_patients(doc)
    assert detail.no_show_count == 1
    assert detail.no_show_count == row.no_show_count


def test_get_patient_foreign_row_not_found(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    cp = _seed_register(db, other, full_name="Theirs")
    with pytest.raises(SehatyNotFoundError):
        PatientRegisterController.get_patient(doc, cp)


# --------------------------------------------------------------------------- #
# add_patient / update_patient
# --------------------------------------------------------------------------- #


def test_add_patient_walkin(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    row = PatientRegisterController.add_patient(
        doc, created_by=doc, full_name="Walk In", phone="0699", tags=["chronic"]
    )
    assert row.user_id is None
    assert row.full_name == "Walk In"
    assert row.total_visits == 0
    with db() as s:
        cp = s.get(ClinicPatient, row.id)
        assert cp.user_id is None
        assert cp.created_by == doc
        assert cp.tags == ["chronic"]


def test_add_patient_empty_name_raises(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        PatientRegisterController.add_patient(doc, created_by=doc, full_name="   ")


def test_update_patient_changes_fields(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc, full_name="Amina")
    detail = PatientRegisterController.update_patient(
        doc, cp, notes="follow up", tags=["vip"], unknown_key="ignored"
    )
    assert detail.notes == "follow up"
    assert detail.tags == ["vip"]
    assert detail.full_name == "Amina"  # untouched


def test_update_patient_foreign_row_not_found(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    cp = _seed_register(db, other, full_name="Theirs")
    with pytest.raises(SehatyNotFoundError):
        PatientRegisterController.update_patient(doc, cp, notes="hack")
