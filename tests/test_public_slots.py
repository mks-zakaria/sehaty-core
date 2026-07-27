"""Pure-SQLite tests for ``DoctorController.get_public_slots``.

This resolver never touches the PostGIS ``geopoint`` column (it projects only
``user_id`` + ``verification_status`` by ``slug``), so it can be exercised on a
throwaway in-memory SQLite engine — no live database required. The real
``doctor_profiles`` table carries a ``Geography`` column that SQLite cannot
compile via ``create_all``, so we hand-create a minimal stand-in with just the
columns the query reads (that's all the resolver looks at anyway).

Covers the verified→slots happy path plus the not-found guard for a PENDING
(never-surfaced) and a missing slug — the same no-existence-leak stance as
``get_by_slug``.
"""

from datetime import UTC, date, datetime, time

import pytest
from sehaty.db import (
    Appointment,
    Availability,
    AvailabilityException,
    Plan,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.doctors import DoctorController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyNotFoundError

# A fixed Monday used as the weekday anchor for the availability window.
_MONDAY = date(2026, 8, 3)
assert _MONDAY.weekday() == 0

# Minimal, geopoint-free stand-in for doctor_profiles: exactly the columns the
# resolver's column-only select reads.
_DOCTOR_PROFILES_DDL = text(
    "CREATE TABLE doctor_profiles ("
    " user_id INTEGER PRIMARY KEY,"
    " full_name VARCHAR(255) NOT NULL,"
    " slug VARCHAR(160) NOT NULL,"
    " license_no VARCHAR(64) NOT NULL,"
    # Slot generation reads the clinic timezone; seed 'UTC' so these local
    # wall-clock windows convert to the same UTC instants the assertions expect.
    " timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',"
    " verification_status VARCHAR(8) NOT NULL)"
)


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SehatyBase.metadata.create_all(
        engine,
        tables=[
            User.__table__,
            Availability.__table__,
            AvailabilityException.__table__,
            Appointment.__table__,
            # Slot generation now consults the subscription: a lapsed doctor
            # keeps their page but loses the booking engine.
            Plan.__table__,
            Subscription.__table__,
        ],
    )
    with engine.begin() as conn:
        conn.execute(_DOCTOR_PROFILES_DDL)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _seed_profile(
    factory: sessionmaker[Session],
    *,
    email: str,
    slug: str,
    license_no: str,
    verification_status: str,
) -> int:
    """Create a DOCTOR user + a doctor_profiles row; return the user id."""
    with factory() as s:
        user = User(email=email, role=UserRole.DOCTOR, is_active=True)
        s.add(user)
        s.commit()
        s.execute(
            text(
                "INSERT INTO doctor_profiles"
                " (user_id, full_name, slug, license_no, verification_status)"
                " VALUES (:u, :n, :sl, :l, :v)"
            ),
            {"u": user.id, "n": "Dr Test", "sl": slug, "l": license_no, "v": verification_status},
        )
        s.commit()
        return user.id


def _add_window(factory: sessionmaker[Session], doctor_id: int) -> None:
    """A Monday 09:00-11:00 window, 30-min slots -> 4 slots."""
    with factory() as s:
        s.add(
            Availability(
                doctor_id=doctor_id,
                weekday=_MONDAY.weekday(),
                start_time=time(9, 0),
                end_time=time(11, 0),
                slot_minutes=30,
            )
        )
        s.commit()


def _subscribe(
    factory: sessionmaker[Session],
    doctor_id: int,
    *,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    period_end: datetime | None = None,
) -> None:
    """Give a doctor a subscription so the booking engine is switched on."""
    with factory() as s:
        plan = s.execute(select(Plan).where(Plan.code == "basic")).scalar_one_or_none()
        if plan is None:
            plan = Plan(code="basic", name="Basic", price_month=199.0, currency="MAD")
            s.add(plan)
            s.flush()
        s.add(
            Subscription(
                doctor_id=doctor_id,
                plan_id=plan.id,
                status=status,
                current_period_start=datetime(2026, 7, 1, tzinfo=UTC),
                current_period_end=period_end or datetime(2026, 12, 31, tzinfo=UTC),
            )
        )
        s.commit()


def test_public_slots_for_verified_doctor(db: sessionmaker[Session]) -> None:
    doc = _seed_profile(
        db, email="v@clinic.ma", slug="dr-verified", license_no="L1", verification_status="VERIFIED"
    )
    _add_window(db, doc)
    _subscribe(db, doc)

    slots = DoctorController.get_public_slots("dr-verified", _MONDAY, _MONDAY)

    assert len(slots) == 4
    assert slots[0] == {
        "start_at": datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        "end_at": datetime(2026, 8, 3, 9, 30, tzinfo=UTC),
    }
    assert slots[-1]["start_at"] == datetime(2026, 8, 3, 10, 30, tzinfo=UTC)


def test_public_slots_pending_not_found(db: sessionmaker[Session]) -> None:
    doc = _seed_profile(
        db, email="p@clinic.ma", slug="dr-pending", license_no="L2", verification_status="PENDING"
    )
    _add_window(db, doc)

    # Profile exists (with real availability) but is PENDING -> never surfaced.
    with pytest.raises(SehatyNotFoundError):
        DoctorController.get_public_slots("dr-pending", _MONDAY, _MONDAY)


def test_public_slots_missing_slug_not_found(db: sessionmaker[Session]) -> None:
    with pytest.raises(SehatyNotFoundError):
        DoctorController.get_public_slots("nobody-here", _MONDAY, _MONDAY)


def test_lapsed_subscription_yields_no_slots(db: sessionmaker[Session]) -> None:
    """An unpaid doctor loses the agenda, not the page.

    `get_public_slots` returns an empty list rather than raising: the profile is
    still public and still callable, so a 404 here would be wrong — it would
    also break the printed QR code in the waiting room.
    """
    doc = _seed_profile(
        db,
        email="lapsed@clinic.ma",
        slug="dr-lapsed",
        license_no="L9",
        verification_status="VERIFIED",
    )
    _add_window(db, doc)
    _subscribe(
        db,
        doc,
        status=SubscriptionStatus.PAST_DUE,
        # Well past the grace window.
        period_end=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert DoctorController.get_public_slots("dr-lapsed", _MONDAY, _MONDAY) == []


def test_doctor_who_never_subscribed_has_no_slots(db: sessionmaker[Session]) -> None:
    # Most doctors on the platform are unclaimed imports; they never had a
    # booking engine and must not appear to have one.
    doc = _seed_profile(
        db,
        email="never@clinic.ma",
        slug="dr-never",
        license_no="L10",
        verification_status="VERIFIED",
    )
    _add_window(db, doc)

    assert DoctorController.get_public_slots("dr-never", _MONDAY, _MONDAY) == []
