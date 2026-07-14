"""Accreditation core tests on an in-memory SQLite engine.

Mirrors ``test_auth.py``: the PostGIS ``Geography`` column + gist index on
``DoctorProfile`` are not buildable on stock SQLite, so this module registers
dialect-scoped compilation shims (geo type -> ``TEXT``; ``ST_GeogFromText`` ->
pass-through) purely for the test engine. Controller reads/writes use
column-only projections, so ``geopoint`` is never selected.
"""

import pytest
from geoalchemy2 import Geography
from geoalchemy2 import functions as geo_functions
from sehaty.db import AuditLog, DoctorProfile, User, UserRole, VerificationStatus
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.admin import AdminController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyNotFoundError


@compiles(Geography, "sqlite")
def _geography_as_text_on_sqlite(element, compiler, **kw) -> str:
    # SQLite has no geography type; store the column as opaque TEXT for tests.
    return "TEXT"


@compiles(geo_functions.ST_GeogFromText, "sqlite")
def _geog_bind_passthrough_on_sqlite(element, compiler, **kw) -> str:
    # Skip the PostGIS constructor SQLite lacks; bind the raw value instead.
    return compiler.process(list(element.clauses)[0], **kw)


_TABLES = [
    User.__table__,
    DoctorProfile.__table__,
    AuditLog.__table__,
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


def _seed_doctor(
    factory: sessionmaker[Session],
    *,
    email: str,
    license_no: str,
    status: VerificationStatus = VerificationStatus.PENDING,
    city: str | None = "Casablanca",
) -> int:
    """Create a DOCTOR user + profile and return the shared user_id."""
    with factory() as s:
        user = User(email=email, role=UserRole.DOCTOR, is_active=True)
        s.add(user)
        s.flush()
        s.add(
            DoctorProfile(
                user_id=user.id,
                full_name="Dr Test",
                slug=f"dr-{license_no.lower()}",
                license_no=license_no,
                city=city,
                verification_status=status,
            )
        )
        s.commit()
        return user.id


def _status(factory: sessionmaker[Session], user_id: int) -> VerificationStatus:
    with factory() as s:
        return s.execute(
            select(DoctorProfile.verification_status).where(DoctorProfile.user_id == user_id)
        ).scalar_one()


def test_list_pending_includes_pending_doctor(db: sessionmaker[Session]) -> None:
    uid = _seed_doctor(db, email="pending@clinic.ma", license_no="LIC-P1")
    _seed_doctor(
        db,
        email="verified@clinic.ma",
        license_no="LIC-V1",
        status=VerificationStatus.VERIFIED,
    )

    pending = AdminController.list_pending_professionals()

    assert [p.user_id for p in pending] == [uid]
    row = pending[0]
    assert row.email == "pending@clinic.ma"
    assert row.license_no == "LIC-P1"
    assert row.full_name == "Dr Test"
    assert row.city == "Casablanca"
    assert row.speciality is None


def test_accredit_verifies_and_audits(db: sessionmaker[Session]) -> None:
    uid = _seed_doctor(db, email="acc@clinic.ma", license_no="LIC-A1")
    assert not AdminController.is_doctor_verified(uid)

    AdminController.accredit(admin_id=999, user_id=uid)

    assert _status(db, uid) == VerificationStatus.VERIFIED
    assert AdminController.is_doctor_verified(uid)
    # Once verified it drops out of the pending queue.
    assert uid not in [p.user_id for p in AdminController.list_pending_professionals()]

    with db() as s:
        log = s.execute(select(AuditLog).where(AuditLog.entity_id == uid)).scalar_one()
        assert log.action == "ACCREDIT"
        assert log.entity == "doctor_profile"
        assert log.actor_user_id == 999


def test_revoke_rejects_and_audits(db: sessionmaker[Session]) -> None:
    uid = _seed_doctor(
        db,
        email="rev@clinic.ma",
        license_no="LIC-R1",
        status=VerificationStatus.VERIFIED,
    )

    AdminController.revoke(admin_id=1, user_id=uid)

    assert _status(db, uid) == VerificationStatus.REJECTED
    assert not AdminController.is_doctor_verified(uid)
    with db() as s:
        log = s.execute(select(AuditLog).where(AuditLog.entity_id == uid)).scalar_one()
        assert log.action == "REVOKE"


def test_accredit_missing_doctor_raises(db: sessionmaker[Session]) -> None:
    with pytest.raises(SehatyNotFoundError):
        AdminController.accredit(admin_id=1, user_id=424242)


def test_accredit_patient_raises(db: sessionmaker[Session]) -> None:
    # A PATIENT has no DoctorProfile, so accreditation must 404.
    with db() as s:
        patient = User(email="pat@x.io", role=UserRole.PATIENT, is_active=True)
        s.add(patient)
        s.commit()
        pid = patient.id
    with pytest.raises(SehatyNotFoundError):
        AdminController.accredit(admin_id=1, user_id=pid)
