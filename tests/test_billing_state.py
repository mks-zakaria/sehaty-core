"""What a doctor is told when the trial runs out.

Until this existed the app could not answer the only question a lapsing doctor
has — "does my agenda still work" — so they saw an empty week and concluded the
product was broken rather than that a bill was due.

Grace and expired are kept apart deliberately. One means "still running, pay
this week", the other means "it has stopped, here is how to restart it", and
collapsing them tells half the doctors the wrong thing.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sehaty.db import DoctorProfile, Plan, Subscription, SubscriptionStatus, User, UserRole
from sqlalchemy.orm import Session

from sehaty.core.controllers.billing import BillingController

NOW = datetime.now(UTC)


def _doctor_with(session: Session, *, email: str, status, period_end) -> int:
    user = User(email=email, role=UserRole.DOCTOR, is_active=True)
    session.add(user)
    session.commit()
    session.add(
        DoctorProfile(
            user_id=user.id,
            full_name="Dr Test",
            slug=email.split("@")[0],
            license_no=f"LIC-{user.id}",
            city="Casablanca",
        )
    )
    plan = session.query(Plan).first()
    if plan is None:
        plan = Plan(code="pack", name="Pack", price_month=199, is_active=True)
        session.add(plan)
        session.commit()
    if status is not None:
        session.add(
            Subscription(
                doctor_id=user.id,
                plan_id=plan.id,
                status=status,
                current_period_start=period_end - timedelta(days=30),
                current_period_end=period_end,
            )
        )
    session.commit()
    return int(user.id)


@pytest.mark.usefixtures("_pg_engine")
class TestBillingState:
    def test_a_doctor_who_never_subscribed_is_a_state_not_an_error(
        self, pg_session: Session
    ) -> None:
        """Most doctors never subscribed; the app must render that, not fail."""
        uid = _doctor_with(pg_session, email="none@c.ma", status=None, period_end=NOW)

        summary = BillingController.subscription_status(uid)

        assert summary.state == "never_subscribed"
        assert summary.booking_enabled is False
        assert summary.plan is None

    def test_an_active_subscription_says_so(self, pg_session: Session) -> None:
        uid = _doctor_with(
            pg_session,
            email="live@c.ma",
            status=SubscriptionStatus.ACTIVE,
            period_end=NOW + timedelta(days=25),
        )

        summary = BillingController.subscription_status(uid)

        assert summary.state == "active"
        assert summary.booking_enabled is True

    def test_the_last_week_is_flagged_before_anything_breaks(self, pg_session: Session) -> None:
        """A warning after the agenda stops is a warning that arrived too late."""
        uid = _doctor_with(
            pg_session,
            email="soon@c.ma",
            status=SubscriptionStatus.ACTIVE,
            period_end=NOW + timedelta(days=3),
        )

        summary = BillingController.subscription_status(uid)

        assert summary.state == "expiring_soon"
        assert summary.booking_enabled is True

    def test_grace_still_books_and_says_it_is_grace(self, pg_session: Session) -> None:
        """Different message from expired: still running, pay this week."""
        uid = _doctor_with(
            pg_session,
            email="grace@c.ma",
            status=SubscriptionStatus.ACTIVE,
            period_end=NOW - timedelta(days=2),
        )

        summary = BillingController.subscription_status(uid)

        assert summary.state == "grace"
        assert summary.in_grace_period is True
        assert summary.booking_enabled is True

    def test_expired_stops_booking_and_says_so(self, pg_session: Session) -> None:
        uid = _doctor_with(
            pg_session,
            email="over@c.ma",
            status=SubscriptionStatus.ACTIVE,
            period_end=NOW - timedelta(days=40),
        )

        summary = BillingController.subscription_status(uid)

        assert summary.state == "expired"
        assert summary.booking_enabled is False
        assert summary.days_remaining is not None and summary.days_remaining < 0
