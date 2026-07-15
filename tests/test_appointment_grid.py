"""Doctor appointment-grid tests on an in-memory SQLite engine.

Covers ``AppointmentController.list_for_doctor``: the patient-name fallback
chain (register full_name -> register email/phone -> booking-user email/phone ->
``Patient #id``), the half-open ``date_from``/``date_to`` filter, ``start_at``
ordering, and doctor scoping. Only the tables this feature touches (users,
clinic_patients, appointments, availabilities) are created — none carry the
PostGIS ``geopoint`` column, so the column-only projection runs on SQLite.
"""

from datetime import UTC, datetime

import pytest
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    Availability,
    ClinicPatient,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.appointments import AppointmentController
from sehaty.core.db import session as session_mod

_TABLES = [
    User.__table__,
    Availability.__table__,
    ClinicPatient.__table__,
    Appointment.__table__,
]

# Minimal, geopoint-free stand-in for doctor_profiles: SQLite cannot compile the
# real table's PostGIS ``Geography`` column via ``create_all``, and
# ``list_for_patient_view`` only projects ``full_name`` (never ``geopoint``).
_DOCTOR_PROFILES_DDL = text(
    "CREATE TABLE doctor_profiles ("
    " user_id INTEGER PRIMARY KEY,"
    " full_name VARCHAR(255) NOT NULL,"
    " slug VARCHAR(160) NOT NULL,"
    " license_no VARCHAR(64) NOT NULL,"
    " verification_status VARCHAR(8) NOT NULL DEFAULT 'PENDING')"
)

_BASE = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    with engine.begin() as conn:
        conn.execute(_DOCTOR_PROFILES_DDL)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _seed_doctor_profile(
    factory: sessionmaker[Session], *, user_id: int, full_name: str, slug: str
) -> None:
    with factory() as s:
        s.execute(
            text(
                "INSERT INTO doctor_profiles (user_id, full_name, slug, license_no)"
                " VALUES (:u, :n, :sl, :l)"
            ),
            {"u": user_id, "n": full_name, "sl": slug, "l": f"LIC-{user_id}"},
        )
        s.commit()


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
    clinic_patient_id: int | None,
    status: AppointmentStatus = AppointmentStatus.REQUESTED,
    reason: str | None = None,
) -> int:
    with factory() as s:
        appt = Appointment(
            patient_id=patient_id,
            doctor_id=doctor_id,
            start_at=start_at,
            end_at=start_at,
            status=status,
            reason=reason,
            clinic_patient_id=clinic_patient_id,
        )
        s.add(appt)
        s.commit()
        return appt.id


def test_name_resolves_to_register_full_name(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    cp = _seed_register(
        db, doctor_id=doc, user_id=pat, full_name="Amina Zahra", email="pat@clinic.ma"
    )
    _seed_appt(
        db, doctor_id=doc, patient_id=pat, start_at=_BASE, clinic_patient_id=cp, reason="fever"
    )

    rows = AppointmentController.list_for_doctor(doc)
    assert len(rows) == 1
    assert rows[0].patient_name == "Amina Zahra"
    assert rows[0].clinic_patient_id == cp
    assert rows[0].reason == "fever"
    assert rows[0].status == str(AppointmentStatus.REQUESTED)


def test_name_falls_back_to_register_email_then_phone(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    # Register row with only an email (no full_name) -> returns the email.
    cp_email = _seed_register(db, doctor_id=doc, user_id=pat, email="only.email@clinic.ma")
    _seed_appt(db, doctor_id=doc, patient_id=pat, start_at=_BASE, clinic_patient_id=cp_email)
    # Register row with only a phone -> returns the phone.
    cp_phone = _seed_register(db, doctor_id=doc, phone="+212600000001")
    _seed_appt(
        db,
        doctor_id=doc,
        patient_id=pat,
        start_at=_BASE.replace(hour=10),
        clinic_patient_id=cp_phone,
    )

    rows = AppointmentController.list_for_doctor(doc)
    assert [r.patient_name for r in rows] == ["only.email@clinic.ma", "+212600000001"]
    assert rows[1].patient_phone == "+212600000001"


def test_name_falls_back_to_user_when_no_register_link(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(
        db, email="user.contact@clinic.ma", phone="+212611111111", role=UserRole.PATIENT
    )
    # No clinic_patient link -> resolves via the booking User.
    _seed_appt(db, doctor_id=doc, patient_id=pat, start_at=_BASE, clinic_patient_id=None)

    rows = AppointmentController.list_for_doctor(doc)
    assert len(rows) == 1
    assert rows[0].clinic_patient_id is None
    assert rows[0].patient_name == "user.contact@clinic.ma"
    assert rows[0].patient_phone == "+212611111111"


def test_name_falls_back_to_patient_id_label(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    # No register link and no matching User row -> "Patient #id".
    ghost = 909090
    _seed_appt(db, doctor_id=doc, patient_id=ghost, start_at=_BASE, clinic_patient_id=None)

    rows = AppointmentController.list_for_doctor(doc)
    assert len(rows) == 1
    assert rows[0].patient_name == f"Patient #{ghost}"
    assert rows[0].patient_phone is None


def test_date_range_filter_is_half_open_and_ordered(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    cp = _seed_register(db, doctor_id=doc, user_id=pat, full_name="Karim B")
    # Seed three appointments an hour apart, inserted out of order.
    a10 = _seed_appt(
        db, doctor_id=doc, patient_id=pat, start_at=_BASE.replace(hour=10), clinic_patient_id=cp
    )
    a9 = _seed_appt(
        db, doctor_id=doc, patient_id=pat, start_at=_BASE.replace(hour=9), clinic_patient_id=cp
    )
    a11 = _seed_appt(
        db, doctor_id=doc, patient_id=pat, start_at=_BASE.replace(hour=11), clinic_patient_id=cp
    )

    # No filter -> all three, ordered by start_at.
    assert [r.id for r in AppointmentController.list_for_doctor(doc)] == [a9, a10, a11]

    # [10:00, 11:00) -> only the 10:00 appointment (11:00 excluded, 9:00 excluded).
    window = AppointmentController.list_for_doctor(
        doc, date_from=_BASE.replace(hour=10), date_to=_BASE.replace(hour=11)
    )
    assert [r.id for r in window] == [a10]

    # from-only is inclusive at the bound.
    assert [
        r.id for r in AppointmentController.list_for_doctor(doc, date_from=_BASE.replace(hour=10))
    ] == [
        a10,
        a11,
    ]
    # to-only is exclusive at the bound.
    assert [
        r.id for r in AppointmentController.list_for_doctor(doc, date_to=_BASE.replace(hour=10))
    ] == [a9]


def test_scoped_to_doctor(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    other = _seed_user(db, email="other@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    cp = _seed_register(db, doctor_id=doc, user_id=pat, full_name="Mine")
    cp_other = _seed_register(db, doctor_id=other, user_id=pat, full_name="Theirs")
    mine = _seed_appt(db, doctor_id=doc, patient_id=pat, start_at=_BASE, clinic_patient_id=cp)
    _seed_appt(db, doctor_id=other, patient_id=pat, start_at=_BASE, clinic_patient_id=cp_other)

    rows = AppointmentController.list_for_doctor(doc)
    assert [r.id for r in rows] == [mine]
    assert rows[0].patient_name == "Mine"


# --------------------------------------------------------------------------- #
# Patient view: list_for_patient_view (doctor_name resolution)
# --------------------------------------------------------------------------- #


def test_patient_view_resolves_doctor_name_from_profile(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    _seed_doctor_profile(db, user_id=doc, full_name="Dr Yassine Alaoui", slug="yassine-alaoui")
    _seed_appt(
        db, doctor_id=doc, patient_id=pat, start_at=_BASE, clinic_patient_id=None, reason="checkup"
    )

    rows = AppointmentController.list_for_patient_view(pat)
    assert len(rows) == 1
    assert rows[0].doctor_id == doc
    assert rows[0].doctor_name == "Dr Yassine Alaoui"
    assert rows[0].reason == "checkup"
    assert rows[0].status == str(AppointmentStatus.REQUESTED)


def test_patient_view_falls_back_to_doctor_id_label(db: sessionmaker[Session]) -> None:
    # Doctor user exists but has NO doctor_profiles row -> "Doctor #id".
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    _seed_appt(db, doctor_id=doc, patient_id=pat, start_at=_BASE, clinic_patient_id=None)

    rows = AppointmentController.list_for_patient_view(pat)
    assert len(rows) == 1
    assert rows[0].doctor_name == f"Doctor #{doc}"


def test_patient_view_ordered_and_scoped_to_patient(db: sessionmaker[Session]) -> None:
    doc1 = _seed_user(db, email="doc1@clinic.ma", role=UserRole.DOCTOR)
    doc2 = _seed_user(db, email="doc2@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    other = _seed_user(db, email="other@clinic.ma", role=UserRole.PATIENT)
    _seed_doctor_profile(db, user_id=doc1, full_name="Dr One", slug="one")
    _seed_doctor_profile(db, user_id=doc2, full_name="Dr Two", slug="two")

    # Inserted out of order; expect start_at ordering.
    a11 = _seed_appt(
        db, doctor_id=doc2, patient_id=pat, start_at=_BASE.replace(hour=11), clinic_patient_id=None
    )
    a9 = _seed_appt(
        db, doctor_id=doc1, patient_id=pat, start_at=_BASE.replace(hour=9), clinic_patient_id=None
    )
    # Another patient's appointment must not appear.
    _seed_appt(db, doctor_id=doc1, patient_id=other, start_at=_BASE, clinic_patient_id=None)

    rows = AppointmentController.list_for_patient_view(pat)
    assert [r.id for r in rows] == [a9, a11]
    assert [r.doctor_name for r in rows] == ["Dr One", "Dr Two"]
