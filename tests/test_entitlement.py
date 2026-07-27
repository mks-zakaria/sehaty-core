"""Tests for subscription-driven entitlement.

The rule under test is commercial: an unpaid subscription switches off the
**booking engine** and nothing else. Several of these assert what must *not*
happen — the page staying live is the promise printed on the sales sheet, and a
regression here would break QR plaques already hanging in waiting rooms.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sehaty.db import Plan, Subscription, SubscriptionStatus, User, UserRole
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.db import session as session_mod
from sehaty.core.services.entitlement import (
    GRACE_DAYS,
    booking_enabled,
    entitlement_for,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
_TABLES = [User.__table__, Plan.__table__, Subscription.__table__]


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


def _doctor(factory: sessionmaker[Session], email: str = "doc@c.ma") -> int:
    with factory() as s:
        user = User(email=email, role=UserRole.DOCTOR, is_active=True)
        s.add(user)
        s.commit()
        return int(user.id)


def _subscribe(
    factory: sessionmaker[Session],
    doctor_id: int,
    *,
    status: SubscriptionStatus,
    period_end: datetime,
) -> None:
    with factory() as s:
        # Unique per call: a renewal test subscribes the same doctor twice.
        plan = Plan(code=f"p{doctor_id}-{period_end:%Y%m%d}", name="Basic", price_month=199.0)
        s.add(plan)
        s.flush()
        s.add(
            Subscription(
                doctor_id=doctor_id,
                plan_id=plan.id,
                status=status,
                current_period_start=period_end - timedelta(days=30),
                current_period_end=period_end,
            )
        )
        s.commit()


class TestEntitled:
    @pytest.mark.parametrize(
        "status",
        [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING],
        ids=["active", "trialing"],
    )
    def test_a_current_subscription_enables_booking(
        self, db: sessionmaker[Session], status: SubscriptionStatus
    ) -> None:
        # TRIALING counts: the three free months are sold as the real product,
        # not a crippled preview.
        doctor = _doctor(db)
        _subscribe(db, doctor, status=status, period_end=NOW + timedelta(days=10))

        result = entitlement_for(doctor, now=NOW)
        assert result.booking_enabled is True
        assert result.reason == "active"


class TestLapsed:
    def test_an_expired_period_disables_booking(self, db: sessionmaker[Session]) -> None:
        doctor = _doctor(db)
        _subscribe(
            db,
            doctor,
            status=SubscriptionStatus.ACTIVE,
            period_end=NOW - timedelta(days=GRACE_DAYS + 1),
        )

        result = entitlement_for(doctor, now=NOW)
        assert result.booking_enabled is False
        assert result.reason == "expired"

    def test_a_doctor_who_never_subscribed_has_no_booking(self, db: sessionmaker[Session]) -> None:
        # Most doctors are unclaimed imports. Not an error, just no engine.
        doctor = _doctor(db)
        result = entitlement_for(doctor, now=NOW)
        assert result.booking_enabled is False
        assert result.reason == "no_subscription"
        assert result.status is None

    def test_past_due_keeps_working_inside_the_grace_window(
        self, db: sessionmaker[Session]
    ) -> None:
        # A cheque that clears on Tuesday must not cost the cabinet its agenda
        # on Monday; chasing a late payment is a phone call, not an outage.
        doctor = _doctor(db)
        _subscribe(
            db,
            doctor,
            status=SubscriptionStatus.PAST_DUE,
            period_end=NOW - timedelta(days=GRACE_DAYS - 1),
        )

        result = entitlement_for(doctor, now=NOW)
        assert result.booking_enabled is True
        assert result.in_grace_period is True

    def test_past_due_stops_after_the_grace_window(self, db: sessionmaker[Session]) -> None:
        doctor = _doctor(db)
        _subscribe(
            db,
            doctor,
            status=SubscriptionStatus.PAST_DUE,
            period_end=NOW - timedelta(days=GRACE_DAYS + 1),
        )

        result = entitlement_for(doctor, now=NOW)
        assert result.booking_enabled is False
        assert result.reason == "past_due"

    def test_cancelling_runs_to_the_end_of_the_paid_period(self, db: sessionmaker[Session]) -> None:
        # They paid for the month; cancelling is not a refund request.
        doctor = _doctor(db)
        _subscribe(
            db,
            doctor,
            status=SubscriptionStatus.CANCELLED,
            period_end=NOW + timedelta(days=5),
        )

        assert entitlement_for(doctor, now=NOW).booking_enabled is True

    def test_a_cancelled_subscription_stops_when_the_period_ends(
        self, db: sessionmaker[Session]
    ) -> None:
        doctor = _doctor(db)
        _subscribe(
            db,
            doctor,
            status=SubscriptionStatus.CANCELLED,
            period_end=NOW - timedelta(days=1),
        )

        result = entitlement_for(doctor, now=NOW)
        assert result.booking_enabled is False
        assert result.reason == "cancelled"


class TestRenewal:
    def test_the_latest_period_wins(self, db: sessionmaker[Session]) -> None:
        # A renewed doctor has an old expired row and a new current one; reading
        # the wrong one would switch off a cabinet that just paid.
        doctor = _doctor(db)
        _subscribe(
            db, doctor, status=SubscriptionStatus.ACTIVE, period_end=NOW - timedelta(days=60)
        )
        _subscribe(
            db, doctor, status=SubscriptionStatus.ACTIVE, period_end=NOW + timedelta(days=30)
        )

        assert booking_enabled(doctor, now=NOW) is True


class TestIsolation:
    def test_one_doctor_lapsing_does_not_affect_another(self, db: sessionmaker[Session]) -> None:
        paid = _doctor(db, "paid@c.ma")
        lapsed = _doctor(db, "lapsed@c.ma")
        _subscribe(db, paid, status=SubscriptionStatus.ACTIVE, period_end=NOW + timedelta(days=30))
        _subscribe(
            db,
            lapsed,
            status=SubscriptionStatus.ACTIVE,
            period_end=NOW - timedelta(days=90),
        )

        assert booking_enabled(paid, now=NOW) is True
        assert booking_enabled(lapsed, now=NOW) is False
