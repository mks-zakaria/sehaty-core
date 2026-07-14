"""Notification-emit integration tests on an in-memory SQLite engine.

These exercise the real controllers (booking, appointment transitions, reviews,
cash payments, referrals) and assert that each business event drops the right
in-app ``Notification`` into the recipient's feed via ``NotificationController``.
The fixture builds the superset of tables all those flows touch plus
``notifications``. ``DoctorProfile`` carries the PostGIS ``geopoint`` column,
which stock SQLite cannot compile, so — like ``test_referral.py`` — a
dialect-scoped shim maps the geo type to ``TEXT`` for the test engine.
"""

import json
from datetime import UTC, date, datetime, time, timedelta

import pytest
from geoalchemy2 import Geography
from sehaty.db import (
    AdminConfig,
    Appointment,
    AppointmentStatus,
    AuditLog,
    Availability,
    ClinicPatient,
    CreditLedger,
    DoctorProfile,
    Invoice,
    Notification,
    Payment,
    Plan,
    Referral,
    ReputationScore,
    Review,
    ReviewDirection,
    Subscription,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.appointments import AppointmentController
from sehaty.core.controllers.availability import AvailabilityController
from sehaty.core.controllers.billing import BillingController
from sehaty.core.controllers.notifications import NotificationController
from sehaty.core.controllers.referral import ReferralController
from sehaty.core.controllers.reviews import ReviewController
from sehaty.core.db import session as session_mod


@compiles(Geography, "sqlite")
def _geography_as_text_on_sqlite(element, compiler, **kw) -> str:
    # SQLite has no geography type; store the column as opaque TEXT for tests.
    return "TEXT"


_TABLES = [
    User.__table__,
    DoctorProfile.__table__,
    Availability.__table__,
    ClinicPatient.__table__,
    Appointment.__table__,
    Review.__table__,
    ReputationScore.__table__,
    Plan.__table__,
    Subscription.__table__,
    Invoice.__table__,
    Payment.__table__,
    CreditLedger.__table__,
    Referral.__table__,
    AdminConfig.__table__,
    Notification.__table__,
    AuditLog.__table__,
]

_MONDAY = date(2026, 8, 3)
assert _MONDAY.weekday() == 0
_SLOT = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
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


def _seed_user(factory: sessionmaker[Session], *, email: str, role: UserRole) -> int:
    with factory() as s:
        user = User(email=email, role=role, is_active=True)
        s.add(user)
        s.commit()
        return user.id


def _seed_doctor_profile(factory: sessionmaker[Session], *, email: str, license_no: str) -> int:
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
                city="Casablanca",
            )
        )
        s.commit()
        return user.id


def _seed_completed_appointment(
    factory: sessionmaker[Session], *, patient_id: int, doctor_id: int
) -> int:
    with factory() as s:
        appt = Appointment(
            patient_id=patient_id,
            doctor_id=doctor_id,
            start_at=_SLOT,
            end_at=_SLOT + timedelta(minutes=30),
            status=AppointmentStatus.COMPLETED,
        )
        s.add(appt)
        s.commit()
        return appt.id


# --------------------------------------------------------------------------- #
# Booking
# --------------------------------------------------------------------------- #


def test_booking_notifies_doctor(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)

    appt = AppointmentController.book(pat, doc, _SLOT, reason="fever")

    feed = NotificationController.list_for(doc)
    assert len(feed) == 1
    n = feed[0]
    assert n.kind == "appointment_booked"
    assert n.entity == "appointment"
    assert n.entity_id == appt.id
    assert n.is_read is False
    # The patient is not notified of their own request.
    assert NotificationController.unread_count(pat) == 0


# --------------------------------------------------------------------------- #
# Transitions
# --------------------------------------------------------------------------- #


def _booked(db: sessionmaker[Session]) -> tuple[int, int, int]:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)
    appt = AppointmentController.book(pat, doc, _SLOT)
    return doc, pat, appt.id


def test_confirm_notifies_patient(db: sessionmaker[Session]) -> None:
    doc, pat, appt_id = _booked(db)
    NotificationController.mark_all_read(doc)  # clear the booking notification

    AppointmentController.transition(doc, UserRole.DOCTOR, appt_id, AppointmentStatus.CONFIRMED)

    feed = NotificationController.list_for(pat)
    assert [n.kind for n in feed] == ["appointment_confirmed"]
    assert feed[0].entity_id == appt_id


def test_cancel_notifies_patient(db: sessionmaker[Session]) -> None:
    doc, pat, appt_id = _booked(db)

    # Patient cancels their own appointment; still the patient is the recipient.
    AppointmentController.transition(pat, UserRole.PATIENT, appt_id, AppointmentStatus.CANCELLED)

    kinds = [n.kind for n in NotificationController.list_for(pat)]
    assert kinds == ["appointment_cancelled"]


# --------------------------------------------------------------------------- #
# Reviews
# --------------------------------------------------------------------------- #


def test_create_notifies_target(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    appt = _seed_completed_appointment(db, patient_id=pat, doctor_id=doc)

    review = ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 5)

    feed = NotificationController.list_for(doc)
    assert [n.kind for n in feed] == ["review_received"]
    assert feed[0].entity == "review"
    assert feed[0].entity_id == review.id
    # The author is not notified of their own review.
    assert NotificationController.unread_count(pat) == 0


def test_reply_notifies_author(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    appt = _seed_completed_appointment(db, patient_id=pat, doctor_id=doc)
    review = ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 5)
    NotificationController.mark_all_read(doc)  # clear the review_received notice

    ReviewController.reply(doc, review.id, "thank you")

    feed = NotificationController.list_for(pat)
    assert [n.kind for n in feed] == ["review_reply"]
    assert feed[0].entity_id == review.id


def test_publish_notifies_author(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    admin = _seed_user(db, email="admin@sehaty.ma", role=UserRole.ADMIN)
    appt = _seed_completed_appointment(db, patient_id=pat, doctor_id=doc)
    review = ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 5)

    ReviewController.moderate(admin, review.id, "PUBLISH")

    kinds = [n.kind for n in NotificationController.list_for(pat)]
    assert kinds == ["review_published"]


def test_remove_does_not_notify(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    admin = _seed_user(db, email="admin@sehaty.ma", role=UserRole.ADMIN)
    appt = _seed_completed_appointment(db, patient_id=pat, doctor_id=doc)
    review = ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 5)

    ReviewController.moderate(admin, review.id, "REMOVE")

    # REMOVE emits nothing to the author (only PUBLISH does).
    assert [n.kind for n in NotificationController.list_for(pat)] == []


# --------------------------------------------------------------------------- #
# Cash payments (+ idempotent replay)
# --------------------------------------------------------------------------- #


def test_cash_payment_notifies_doctor_and_replay_is_silent(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    admin = _seed_user(db, email="admin@sehaty.ma", role=UserRole.ADMIN)
    BillingController.subscribe(doc, "pro")
    invoice = BillingController.generate_invoice(doc)

    BillingController.record_cash_payment(
        admin, invoice.id, 299.0, receipt_no="R-001", paid_at=_NOW
    )

    feed = NotificationController.list_for(doc)
    assert [n.kind for n in feed] == ["payment_recorded"]
    assert feed[0].entity == "invoice"
    assert feed[0].entity_id == invoice.id

    # Replaying the same receipt is an idempotent no-op — no second notification.
    BillingController.record_cash_payment(
        admin, invoice.id, 299.0, receipt_no="R-001", paid_at=_NOW + timedelta(hours=1)
    )
    assert NotificationController.unread_count(doc) == 1


# --------------------------------------------------------------------------- #
# Referral reward
# --------------------------------------------------------------------------- #


def test_rewarded_referral_notifies_referrer(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    admin = _seed_user(db, email="admin@sehaty.ma", role=UserRole.ADMIN)
    referrer = _seed_doctor_profile(db, email="ref@clinic.ma", license_no="LIC-R")
    referred = _seed_doctor_profile(db, email="new@clinic.ma", license_no="LIC-N")
    ReferralController.record_referral(ReferralController.code_for(referrer), referred)

    BillingController.subscribe(referred, "essentiel")
    invoice = BillingController.generate_invoice(referred)
    BillingController.record_cash_payment(
        admin, invoice.id, invoice.amount, receipt_no="R-100", paid_at=_NOW
    )

    feed = NotificationController.list_for(referrer)
    assert "referral_rewarded" in [n.kind for n in feed]
    rewarded = next(n for n in feed if n.kind == "referral_rewarded")
    assert rewarded.entity == "referral"

    # Settling again (referral already REWARDED) notifies nobody a second time.
    before = NotificationController.unread_count(referrer)
    assert ReferralController.settle_first_payment(referred) == 0.0
    assert NotificationController.unread_count(referrer) == before


def test_referral_reward_override_amount_in_message(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    admin = _seed_user(db, email="admin@sehaty.ma", role=UserRole.ADMIN)
    referrer = _seed_doctor_profile(db, email="ref@clinic.ma", license_no="LIC-R")
    referred = _seed_doctor_profile(db, email="new@clinic.ma", license_no="LIC-N")
    with db() as s:
        s.add(AdminConfig(key="referral_reward_mad", value=json.dumps(300.0)))
        s.commit()
    ReferralController.record_referral(ReferralController.code_for(referrer), referred)

    BillingController.subscribe(referred, "essentiel")
    invoice = BillingController.generate_invoice(referred)
    BillingController.record_cash_payment(
        admin, invoice.id, invoice.amount, receipt_no="R-200", paid_at=_NOW
    )

    rewarded = next(
        n for n in NotificationController.list_for(referrer) if n.kind == "referral_rewarded"
    )
    assert "300" in rewarded.message


# --------------------------------------------------------------------------- #
# Non-fatal guarantee
# --------------------------------------------------------------------------- #


def test_notify_failure_never_breaks_booking(
    db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    AvailabilityController.add(doc, 0, time(9, 0), time(12, 0), 30)

    def _boom(*args, **kwargs):
        raise RuntimeError("notification backend down")

    monkeypatch.setattr(NotificationController, "notify", staticmethod(_boom))

    # The booking must still succeed even though the notify raises.
    appt = AppointmentController.book(pat, doc, _SLOT)
    assert appt.status == AppointmentStatus.REQUESTED
    assert appt.id is not None
