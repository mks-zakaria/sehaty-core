"""Reviews + reputation core tests on an in-memory SQLite engine.

Covers the booking gate (completed-only, party-only, stars range), the
one-review-per-(author, appointment, direction) rule, the two-way case (patient
rates doctor *and* doctor rates patient off one appointment), the moderation
lifecycle (PENDING → PUBLISHED/REMOVED with a recomputed ``ReputationScore``),
and the right-of-reply + flag transitions. Only the tables this feature touches
are created; none carry the PostGIS ``geopoint`` column, so no dialect shims are
needed.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    AuditLog,
    ReputationScore,
    Review,
    ReviewDirection,
    ReviewStatus,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.reviews import ReviewController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)

_TABLES = [
    User.__table__,
    Appointment.__table__,
    Review.__table__,
    ReputationScore.__table__,
    AuditLog.__table__,
]

_START = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


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


def _seed_appointment(
    factory: sessionmaker[Session],
    *,
    patient_id: int,
    doctor_id: int,
    status: AppointmentStatus = AppointmentStatus.COMPLETED,
) -> int:
    with factory() as s:
        appt = Appointment(
            patient_id=patient_id,
            doctor_id=doctor_id,
            start_at=_START,
            end_at=_START + timedelta(minutes=30),
            status=status,
        )
        s.add(appt)
        s.commit()
        return appt.id


def _completed_setup(
    factory: sessionmaker[Session],
    *,
    status: AppointmentStatus = AppointmentStatus.COMPLETED,
) -> tuple[int, int, int]:
    """Return (doctor_id, patient_id, appointment_id)."""
    doc = _seed_user(factory, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat = _seed_user(factory, email="pat@clinic.ma", role=UserRole.PATIENT)
    appt = _seed_appointment(factory, patient_id=pat, doctor_id=doc, status=status)
    return doc, pat, appt


# --------------------------------------------------------------------------- #
# Booking gate
# --------------------------------------------------------------------------- #


def test_review_non_completed_conflicts(db: sessionmaker[Session]) -> None:
    doc, pat, appt = _completed_setup(db, status=AppointmentStatus.CONFIRMED)
    with pytest.raises(SehatyConflictError):
        ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 5)


def test_review_non_party_forbidden(db: sessionmaker[Session]) -> None:
    doc, pat, appt = _completed_setup(db)
    stranger = _seed_user(db, email="stranger@clinic.ma", role=UserRole.PATIENT)
    with pytest.raises(SehatyForbiddenError):
        ReviewController.create(stranger, appt, ReviewDirection.PATIENT_ON_DOCTOR, 4)


def test_review_wrong_direction_actor_forbidden(db: sessionmaker[Session]) -> None:
    # The patient trying to author a DOCTOR_ON_PATIENT review is the wrong actor.
    doc, pat, appt = _completed_setup(db)
    with pytest.raises(SehatyForbiddenError):
        ReviewController.create(pat, appt, ReviewDirection.DOCTOR_ON_PATIENT, 3)


def test_review_missing_appointment_not_found(db: sessionmaker[Session]) -> None:
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    with pytest.raises(SehatyNotFoundError):
        ReviewController.create(pat, 424242, ReviewDirection.PATIENT_ON_DOCTOR, 5)


@pytest.mark.parametrize("stars", [0, 6, -1, 10])
def test_review_stars_out_of_range_validation(db: sessionmaker[Session], stars: int) -> None:
    doc, pat, appt = _completed_setup(db)
    with pytest.raises(SehatyValidationError):
        ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, stars)


def test_create_lands_pending_and_audits(db: sessionmaker[Session]) -> None:
    doc, pat, appt = _completed_setup(db)
    review = ReviewController.create(
        pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 5, comment="great"
    )
    assert review.status == ReviewStatus.PENDING
    assert review.target_id == doc
    assert review.author_id == pat
    with db() as s:
        log = s.execute(
            select(AuditLog).where(AuditLog.entity_id == review.id, AuditLog.entity == "review")
        ).scalar_one()
        assert log.action == "REVIEW_CREATE"
        assert log.actor_user_id == pat


# --------------------------------------------------------------------------- #
# One-per-appointment-per-direction + two-way
# --------------------------------------------------------------------------- #


def test_duplicate_same_direction_conflicts(db: sessionmaker[Session]) -> None:
    doc, pat, appt = _completed_setup(db)
    ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 5)
    with pytest.raises(SehatyConflictError):
        ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 4)


def test_two_way_reviews_allowed(db: sessionmaker[Session]) -> None:
    doc, pat, appt = _completed_setup(db)
    r1 = ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 5)
    r2 = ReviewController.create(doc, appt, ReviewDirection.DOCTOR_ON_PATIENT, 4)
    assert r1.target_id == doc and r1.author_id == pat
    assert r2.target_id == pat and r2.author_id == doc


# --------------------------------------------------------------------------- #
# Moderation + reputation
# --------------------------------------------------------------------------- #


def _admin(db: sessionmaker[Session]) -> int:
    return _seed_user(db, email="admin@clinic.ma", role=UserRole.ADMIN)


def test_pending_not_public_then_publish_updates_score(db: sessionmaker[Session]) -> None:
    doc, pat, appt = _completed_setup(db)
    admin = _admin(db)
    review = ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 4)

    # PENDING → not visible publicly.
    assert ReviewController.list_published_for(doc) == []
    assert review.id in [r.id for r in ReviewController.list_moderation_queue()]

    moderated = ReviewController.moderate(admin, review.id, "PUBLISH")
    assert moderated.status == ReviewStatus.PUBLISHED

    published = ReviewController.list_published_for(doc)
    assert [r.id for r in published] == [review.id]

    with db() as s:
        score = s.get(ReputationScore, doc)
        assert score is not None
        assert score.review_count == 1
        assert score.avg_stars == 4.0
        log = s.execute(select(AuditLog).where(AuditLog.action == "REVIEW_MODERATE")).scalar_one()
        assert log.actor_user_id == admin


def test_publish_average_over_multiple(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat1 = _seed_user(db, email="p1@clinic.ma", role=UserRole.PATIENT)
    pat2 = _seed_user(db, email="p2@clinic.ma", role=UserRole.PATIENT)
    admin = _admin(db)
    a1 = _seed_appointment(db, patient_id=pat1, doctor_id=doc)
    a2 = _seed_appointment(db, patient_id=pat2, doctor_id=doc)

    r1 = ReviewController.create(pat1, a1, ReviewDirection.PATIENT_ON_DOCTOR, 5)
    r2 = ReviewController.create(pat2, a2, ReviewDirection.PATIENT_ON_DOCTOR, 3)
    ReviewController.moderate(admin, r1.id, "PUBLISH")
    ReviewController.moderate(admin, r2.id, "PUBLISH")

    with db() as s:
        score = s.get(ReputationScore, doc)
        assert score.review_count == 2
        assert score.avg_stars == 4.0


def test_remove_recomputes_score(db: sessionmaker[Session]) -> None:
    doc = _seed_user(db, email="doc@clinic.ma", role=UserRole.DOCTOR)
    pat1 = _seed_user(db, email="p1@clinic.ma", role=UserRole.PATIENT)
    pat2 = _seed_user(db, email="p2@clinic.ma", role=UserRole.PATIENT)
    admin = _admin(db)
    a1 = _seed_appointment(db, patient_id=pat1, doctor_id=doc)
    a2 = _seed_appointment(db, patient_id=pat2, doctor_id=doc)

    r1 = ReviewController.create(pat1, a1, ReviewDirection.PATIENT_ON_DOCTOR, 5)
    r2 = ReviewController.create(pat2, a2, ReviewDirection.PATIENT_ON_DOCTOR, 1)
    ReviewController.moderate(admin, r1.id, "PUBLISH")
    ReviewController.moderate(admin, r2.id, "PUBLISH")

    # Remove the 1-star; only the 5-star remains published.
    removed = ReviewController.moderate(admin, r2.id, "REMOVE")
    assert removed.status == ReviewStatus.REMOVED

    assert [r.id for r in ReviewController.list_published_for(doc)] == [r1.id]
    with db() as s:
        score = s.get(ReputationScore, doc)
        assert score.review_count == 1
        assert score.avg_stars == 5.0


def test_moderate_non_admin_forbidden(db: sessionmaker[Session]) -> None:
    doc, pat, appt = _completed_setup(db)
    review = ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 4)
    with pytest.raises(SehatyForbiddenError):
        ReviewController.moderate(pat, review.id, "PUBLISH")


def test_moderate_bad_action_validation(db: sessionmaker[Session]) -> None:
    doc, pat, appt = _completed_setup(db)
    admin = _admin(db)
    review = ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 4)
    with pytest.raises(SehatyValidationError):
        ReviewController.moderate(admin, review.id, "DELETE")


# --------------------------------------------------------------------------- #
# Reply + flag
# --------------------------------------------------------------------------- #


def test_only_target_can_reply(db: sessionmaker[Session]) -> None:
    doc, pat, appt = _completed_setup(db)
    review = ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 4)

    # The author (patient) is not the target; forbidden.
    with pytest.raises(SehatyForbiddenError):
        ReviewController.reply(pat, review.id, "thanks")

    replied = ReviewController.reply(doc, review.id, "thank you for your visit")
    assert replied.reply == "thank you for your visit"
    assert replied.reply_at is not None

    # Only once.
    with pytest.raises(SehatyConflictError):
        ReviewController.reply(doc, review.id, "again")


def test_flag_moves_to_flagged_and_audits(db: sessionmaker[Session]) -> None:
    doc, pat, appt = _completed_setup(db)
    review = ReviewController.create(pat, appt, ReviewDirection.PATIENT_ON_DOCTOR, 4)

    flagged = ReviewController.flag(doc, review.id)
    assert flagged.status == ReviewStatus.FLAGGED
    assert review.id in [r.id for r in ReviewController.list_moderation_queue()]
    with db() as s:
        log = s.execute(select(AuditLog).where(AuditLog.action == "REVIEW_FLAG")).scalar_one()
        assert log.actor_user_id == doc
