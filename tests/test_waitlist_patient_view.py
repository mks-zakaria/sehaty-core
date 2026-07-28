"""The patient's own view of the waitlist queues they are in.

Separate from ``test_confirmations.py`` because this read joins
``doctor_profiles`` to name the doctor, and that table carries the PostGIS
``geopoint`` column which stock SQLite cannot compile. So these run against a
real database via ``pg_session`` and skip cleanly when none is reachable.

What is being protected here is small and easy to lose: an offer the patient
cannot see is not an offer. The doctor's screen reports the slot went out and
the queue moves on, while the person it was offered to has nowhere to find it
and watches it expire.
"""

from datetime import UTC, datetime, timedelta

import pytest
from geoalchemy2.elements import WKTElement
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    DoctorProfile,
    Plan,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from sehaty.core.controllers.waitlist import OFFER_TTL_MINUTES, WaitlistController

NOW = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
_LAT, _LNG, _SRID = 33.5731104, -7.5898434, 4326


def _doctor(session: Session, *, email: str, slug: str) -> int:
    user = User(email=email, role=UserRole.DOCTOR, is_active=True)
    session.add(user)
    session.commit()
    session.add(
        DoctorProfile(
            user_id=user.id,
            full_name=slug.replace("-", " ").title(),
            slug=slug,
            license_no=f"LIC-{user.id}",
            city="Casablanca",
            geopoint=WKTElement(f"POINT({_LNG} {_LAT})", srid=_SRID),
        )
    )
    # Joining a waitlist checks the doctor still serves slots, and production
    # opens a trial the moment a doctor is accredited.
    plan = session.execute(select(Plan).limit(1)).scalar_one_or_none()
    if plan is None:
        plan = Plan(code="wl-test", name="Waitlist test", price_month=199, is_active=True)
        session.add(plan)
        session.commit()
    session.add(
        Subscription(
            doctor_id=user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=NOW - timedelta(days=1),
            current_period_end=NOW + timedelta(days=30),
        )
    )
    session.commit()
    return user.id


def _patient(session: Session, email: str) -> int:
    user = User(email=email, role=UserRole.PATIENT, is_active=True)
    session.add(user)
    session.commit()
    return user.id


def _appointment(session: Session, doctor: int, patient: int) -> int:
    appointment = Appointment(
        doctor_id=doctor,
        patient_id=patient,
        start_at=NOW + timedelta(hours=4),
        end_at=NOW + timedelta(hours=4, minutes=30),
        status=AppointmentStatus.CONFIRMED,
    )
    session.add(appointment)
    session.commit()
    return appointment.id


@pytest.mark.usefixtures("_pg_engine")
class TestPatientWaitlistView:
    def test_shows_an_offer_with_its_slot_and_deadline(self, pg_session: Session) -> None:
        doctor = _doctor(pg_session, email="doc-wl@c.ma", slug="dr-amine-tazi")
        booked = _patient(pg_session, "booked-wl@c.ma")
        waiting = _patient(pg_session, "waiting-wl@c.ma")
        WaitlistController.join(doctor, waiting)
        WaitlistController.release_slot(_appointment(pg_session, doctor, booked), now=NOW)

        rows = WaitlistController.for_patient(waiting, now=NOW)

        assert len(rows) == 1
        assert rows[0].status == "OFFERED"
        assert rows[0].doctor_slug == "dr-amine-tazi"
        # Both are required to act: the slot to judge, the deadline to hurry.
        assert rows[0].offered_start_at is not None
        assert rows[0].offer_expires_at == NOW + timedelta(minutes=OFFER_TTL_MINUTES)

    def test_a_plain_wait_has_no_slot_attached(self, pg_session: Session) -> None:
        doctor = _doctor(pg_session, email="doc-wl2@c.ma", slug="dr-sara-bennani")
        waiting = _patient(pg_session, "waiting-wl2@c.ma")
        WaitlistController.join(doctor, waiting)

        (row,) = WaitlistController.for_patient(waiting, now=NOW)

        assert row.status == "WAITING"
        assert row.offered_start_at is None
        assert row.offer_expires_at is None

    def test_returns_only_the_callers_own_entries(self, pg_session: Session) -> None:
        doctor = _doctor(pg_session, email="doc-wl3@c.ma", slug="dr-omar-idrissi")
        mine = _patient(pg_session, "mine-wl@c.ma")
        theirs = _patient(pg_session, "theirs-wl@c.ma")
        WaitlistController.join(doctor, mine)
        WaitlistController.join(doctor, theirs)

        rows = WaitlistController.for_patient(mine, now=NOW)

        assert [r.doctor_id for r in rows] == [doctor]
        assert len(rows) == 1
