"""Admin list-reads (Users + Subscriptions pages) on in-memory SQLite.

Mirrors ``test_admin.py`` / ``test_reporting.py``: the PostGIS ``Geography``
column on ``DoctorProfile`` is not buildable on stock SQLite, so this module
registers dialect-scoped compilation shims (geo type -> ``TEXT``;
``ST_GeogFromText`` -> pass-through) purely for the test engine. The list reads
use column-only projections, so ``geopoint`` itself is never selected.
"""

from datetime import UTC, datetime, timedelta

import pytest
from geoalchemy2 import Geography
from geoalchemy2 import functions as geo_functions
from sehaty.db import (
    DoctorProfile,
    Plan,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
    VerificationStatus,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.admin import AdminController
from sehaty.core.db import session as session_mod


@compiles(Geography, "sqlite")
def _geography_as_text_on_sqlite(element, compiler, **kw) -> str:  # noqa: ANN001
    # SQLite has no geography type; store the column as opaque TEXT for tests.
    return "TEXT"


@compiles(geo_functions.ST_GeogFromText, "sqlite")
def _geog_bind_passthrough_on_sqlite(element, compiler, **kw) -> str:  # noqa: ANN001
    # Skip the PostGIS constructor SQLite lacks; bind the raw value instead.
    return compiler.process(list(element.clauses)[0], **kw)


_TABLES = [
    User.__table__,
    DoctorProfile.__table__,
    Plan.__table__,
    Subscription.__table__,
]

_NOW = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)


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


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> dict[str, int]:
    """Admin, a doctor with a profile + subscription, and a patient.

    ``created_at`` is set explicitly, oldest -> newest (admin, patient, doctor),
    so newest-first ordering is deterministic.
    """
    ids: dict[str, int] = {}
    with db() as s:
        admin = User(
            email="admin@sehaty.ma",
            role=UserRole.ADMIN,
            is_active=True,
            created_at=_NOW,
        )
        patient = User(
            email="pat@sehaty.ma",
            phone="+212600000001",
            role=UserRole.PATIENT,
            is_active=True,
            created_at=_NOW + timedelta(minutes=1),
        )
        doctor = User(
            email="doc@clinic.ma",
            phone="+212600000002",
            role=UserRole.DOCTOR,
            is_active=False,
            created_at=_NOW + timedelta(minutes=2),
        )
        s.add_all([admin, patient, doctor])
        s.flush()
        ids.update(admin=admin.id, patient=patient.id, doctor=doctor.id)

        s.add(
            DoctorProfile(
                user_id=doctor.id,
                full_name="Dr Amine",
                slug="dr-amine",
                license_no="LIC-1",
                city="Casablanca",
                verification_status=VerificationStatus.VERIFIED,
            )
        )

        plan = Plan(code="pro", name="Pro", price_month=299.0, currency="MAD")
        s.add(plan)
        s.flush()
        ids["plan"] = plan.id

        sub = Subscription(
            doctor_id=doctor.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=_NOW,
            current_period_end=_NOW + timedelta(days=30),
        )
        s.add(sub)
        s.flush()
        ids["sub"] = sub.id
        s.commit()
    return ids


# --------------------------------------------------------------------------- #
# list_users
# --------------------------------------------------------------------------- #


def test_list_users_newest_first_with_names(seeded: dict[str, int]) -> None:
    users = AdminController.list_users()

    # Newest-first: doctor, patient, admin.
    assert [u.id for u in users] == [seeded["doctor"], seeded["patient"], seeded["admin"]]

    by_id = {u.id: u for u in users}
    doctor = by_id[seeded["doctor"]]
    assert doctor.role == str(UserRole.DOCTOR)
    assert doctor.full_name == "Dr Amine"
    assert doctor.is_active is False
    assert doctor.phone == "+212600000002"
    # Patient / admin have no DoctorProfile, so no full_name.
    assert by_id[seeded["patient"]].full_name is None
    assert by_id[seeded["admin"]].full_name is None


def test_list_users_role_filter_narrows_to_doctors(seeded: dict[str, int]) -> None:
    doctors = AdminController.list_users(role=str(UserRole.DOCTOR))
    assert [u.id for u in doctors] == [seeded["doctor"]]
    assert doctors[0].full_name == "Dr Amine"


def test_list_users_is_active_filter(seeded: dict[str, int]) -> None:
    inactive = AdminController.list_users(is_active=False)
    assert [u.id for u in inactive] == [seeded["doctor"]]

    active = AdminController.list_users(is_active=True)
    assert {u.id for u in active} == {seeded["admin"], seeded["patient"]}


def test_list_users_empty(db: sessionmaker[Session]) -> None:
    assert AdminController.list_users() == []


# --------------------------------------------------------------------------- #
# list_subscriptions
# --------------------------------------------------------------------------- #


def test_list_subscriptions_carries_doctor_and_plan(seeded: dict[str, int]) -> None:
    subs = AdminController.list_subscriptions()
    assert len(subs) == 1
    row = subs[0]
    assert row.id == seeded["sub"]
    assert row.doctor_id == seeded["doctor"]
    assert row.doctor_name == "Dr Amine"
    assert row.plan_code == "pro"
    assert row.plan_name == "Pro"
    assert row.price_month == 299.0
    assert row.currency == "MAD"
    assert row.status == str(SubscriptionStatus.ACTIVE)


def test_list_subscriptions_status_filter(seeded: dict[str, int]) -> None:
    assert len(AdminController.list_subscriptions(status=str(SubscriptionStatus.ACTIVE))) == 1
    assert AdminController.list_subscriptions(status=str(SubscriptionStatus.CANCELLED)) == []
