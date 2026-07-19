"""Reviews + reputation business logic (two-way, booking-gated, moderated).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A review
is only possible off the back of a *completed* appointment the author was a party
to (the "booking gate"); patient-on-doctor and doctor-on-patient are two distinct
directions, so the same appointment yields at most one review per party. Fresh
reviews land as ``PENDING`` and are invisible until an admin moderates them;
publishing (or removing) recomputes the rated user's materialized
``ReputationScore``. Every state-changing action writes an immutable ``AuditLog``
entry. Failures raise the ``SehatyError`` taxonomy; methods never return ``None``
to signal an error.
"""

from datetime import datetime

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
from sehaty.db.base import utcnow
from sqlalchemy import func, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)

_MODERATION_ACTIONS = {"PUBLISH", "REMOVE"}


class ReviewRow(DomainModel):
    """A single review as seen on the wire (detached projection).

    The id-based view returned by :meth:`ReviewController.create` / :meth:`reply`
    / :meth:`flag` / :meth:`moderate` / :meth:`list_published_for` /
    :meth:`list_moderation_queue` — the transport layer serialises it directly,
    replacing the former ``sehaty-api`` ``ReviewOut`` mirror.
    """

    id: int
    author_id: int
    target_id: int
    appointment_id: int
    direction: ReviewDirection
    stars: int
    comment: str | None
    status: ReviewStatus
    reply: str | None
    reply_at: datetime | None
    created_at: datetime


class ReviewController:
    @staticmethod
    def create(
        author_id: int,
        appointment_id: int,
        direction: ReviewDirection,
        stars: int,
        comment: str | None = None,
    ) -> ReviewRow:
        """Create a ``PENDING`` review behind the booking gate.

        The appointment must exist, be ``COMPLETED``, and the author must be the
        party the ``direction`` names (``PATIENT_ON_DOCTOR`` → author is the
        patient and the target is the doctor; ``DOCTOR_ON_PATIENT`` → author is
        the doctor and the target is the patient). A wrong actor/direction is
        ``SehatyForbiddenError``; an appointment that has not completed is
        ``SehatyConflictError``; ``stars`` outside 1..5 is
        ``SehatyValidationError``. At most one review per
        (author, appointment, direction) — a duplicate is ``SehatyConflictError``.
        Records a ``REVIEW_CREATE`` audit entry and returns the review.
        """
        if not 1 <= stars <= 5:
            raise SehatyValidationError("stars must be between 1 and 5")

        with get_session() as session:
            appt = session.get(Appointment, appointment_id)
            if appt is None:
                raise SehatyNotFoundError(f"no appointment {appointment_id}")

            # Booking gate: the direction fixes who may author and who is rated.
            if direction == ReviewDirection.PATIENT_ON_DOCTOR:
                expected_author, target_id = appt.patient_id, appt.doctor_id
            else:  # DOCTOR_ON_PATIENT
                expected_author, target_id = appt.doctor_id, appt.patient_id
            if author_id != expected_author:
                raise SehatyForbiddenError("not a party to this appointment for this direction")

            if appt.status != AppointmentStatus.COMPLETED:
                raise SehatyConflictError("appointment is not completed")

            # One review per (author, appointment, direction) — pre-check the
            # unique constraint so we raise a clean conflict.
            duplicate = session.execute(
                select(Review.id).where(
                    Review.author_id == author_id,
                    Review.appointment_id == appointment_id,
                    Review.direction == direction,
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                raise SehatyConflictError("already reviewed this appointment in this direction")

            review = Review(
                author_id=author_id,
                target_id=target_id,
                appointment_id=appointment_id,
                direction=direction,
                stars=stars,
                comment=comment,
                status=ReviewStatus.PENDING,
            )
            session.add(review)
            session.flush()
            session.add(
                AuditLog(
                    actor_user_id=author_id,
                    action="REVIEW_CREATE",
                    entity="review",
                    entity_id=review.id,
                )
            )
            session.flush()
            notify_target_id = target_id

        # Notify the rated party AFTER the review commits (its own session, never
        # nested). A notification failure must NEVER break review creation, which
        # is already persisted above.
        try:
            from sehaty.core.controllers.notifications import NotificationController

            NotificationController.notify(
                notify_target_id,
                kind="review_received",
                message="You received a new review",
                entity="review",
                entity_id=review.id,
            )
        except Exception:
            pass
        return ReviewRow.model_validate(review)

    @staticmethod
    def reply(user_id: int, review_id: int, text: str) -> ReviewRow:
        """Let the rated party post their one right-of-reply.

        Only the review's ``target`` may reply and only once; anyone else is
        ``SehatyForbiddenError`` and a second reply is ``SehatyConflictError``.
        Sets ``reply`` + ``reply_at`` and records a ``REVIEW_REPLY`` audit entry.
        """
        with get_session() as session:
            review = session.get(Review, review_id)
            if review is None:
                raise SehatyNotFoundError(f"no review {review_id}")
            if review.target_id != user_id:
                raise SehatyForbiddenError("only the rated party may reply")
            if review.reply is not None:
                raise SehatyConflictError("review already has a reply")

            review.reply = text
            review.reply_at = utcnow()
            session.add(
                AuditLog(
                    actor_user_id=user_id,
                    action="REVIEW_REPLY",
                    entity="review",
                    entity_id=review.id,
                )
            )
            session.flush()
            author_id = review.author_id

        # Notify the review author of the reply AFTER it commits (its own session,
        # never nested). A notification failure must NEVER break the reply, which
        # is already persisted above.
        try:
            from sehaty.core.controllers.notifications import NotificationController

            NotificationController.notify(
                author_id,
                kind="review_reply",
                message="Your review received a reply",
                entity="review",
                entity_id=review.id,
            )
        except Exception:
            pass
        return ReviewRow.model_validate(review)

    @staticmethod
    def flag(user_id: int, review_id: int) -> ReviewRow:
        """Flag a review for moderation (any authenticated user).

        Moves the review to ``FLAGGED`` so it surfaces in the moderation queue
        and records a ``REVIEW_FLAG`` audit entry.
        """
        with get_session() as session:
            review = session.get(Review, review_id)
            if review is None:
                raise SehatyNotFoundError(f"no review {review_id}")
            review.status = ReviewStatus.FLAGGED
            session.add(
                AuditLog(
                    actor_user_id=user_id,
                    action="REVIEW_FLAG",
                    entity="review",
                    entity_id=review.id,
                )
            )
            session.flush()
            return ReviewRow.model_validate(review)

    @staticmethod
    def moderate(admin_id: int, review_id: int, action: str) -> ReviewRow:
        """Publish or remove a review (ADMIN only), recomputing reputation.

        ``action`` must be ``"PUBLISH"`` or ``"REMOVE"`` (else
        ``SehatyValidationError``); a non-admin actor is ``SehatyForbiddenError``.
        ``PUBLISH`` sets ``PUBLISHED``, ``REMOVE`` sets ``REMOVED``; either way the
        target's ``ReputationScore`` is recomputed over their ``PUBLISHED``
        reviews. Records a ``REVIEW_MODERATE`` audit entry.
        """
        if action not in _MODERATION_ACTIONS:
            raise SehatyValidationError(f"unknown moderation action {action!r}")

        with get_session() as session:
            actor_role = session.execute(
                select(User.role).where(User.id == admin_id)
            ).scalar_one_or_none()
            if actor_role != UserRole.ADMIN:
                raise SehatyForbiddenError("only an admin may moderate reviews")

            review = session.get(Review, review_id)
            if review is None:
                raise SehatyNotFoundError(f"no review {review_id}")

            review.status = ReviewStatus.PUBLISHED if action == "PUBLISH" else ReviewStatus.REMOVED
            session.flush()
            ReviewController._recompute_reputation(session, review.target_id)
            session.add(
                AuditLog(
                    actor_user_id=admin_id,
                    action="REVIEW_MODERATE",
                    entity="review",
                    entity_id=review.id,
                    meta=action,
                )
            )
            session.flush()
            notify_author_id = review.author_id

        # On PUBLISH, notify the author their review is now live — AFTER the
        # moderation commits (its own session, never nested). REMOVE emits
        # nothing. A notification failure must NEVER break moderation, which is
        # already persisted above.
        if action == "PUBLISH":
            try:
                from sehaty.core.controllers.notifications import NotificationController

                NotificationController.notify(
                    notify_author_id,
                    kind="review_published",
                    message="Your review has been published",
                    entity="review",
                    entity_id=review.id,
                )
            except Exception:
                pass
        return ReviewRow.model_validate(review)

    @staticmethod
    def list_published_for(target_id: int) -> list[ReviewRow]:
        """Return a user's ``PUBLISHED`` reviews (public), newest first."""
        stmt = (
            select(Review)
            .where(
                Review.target_id == target_id,
                Review.status == ReviewStatus.PUBLISHED,
            )
            .order_by(Review.created_at.desc(), Review.id.desc())
        )
        with get_session() as session:
            return [ReviewRow.model_validate(r) for r in session.execute(stmt).scalars().all()]

    @staticmethod
    def list_moderation_queue() -> list[ReviewRow]:
        """Return every ``PENDING`` or ``FLAGGED`` review (admin), newest first."""
        stmt = (
            select(Review)
            .where(Review.status.in_((ReviewStatus.PENDING, ReviewStatus.FLAGGED)))
            .order_by(Review.created_at.desc(), Review.id.desc())
        )
        with get_session() as session:
            return [ReviewRow.model_validate(r) for r in session.execute(stmt).scalars().all()]

    @staticmethod
    def _recompute_reputation(session, target_id: int) -> None:
        """Refresh (upsert) the target's ``ReputationScore`` from PUBLISHED reviews."""
        count, avg = session.execute(
            select(func.count(Review.id), func.avg(Review.stars)).where(
                Review.target_id == target_id,
                Review.status == ReviewStatus.PUBLISHED,
            )
        ).one()

        score = session.get(ReputationScore, target_id)
        if score is None:
            score = ReputationScore(user_id=target_id)
            session.add(score)
        score.review_count = int(count)
        score.avg_stars = float(avg) if avg is not None else 0.0
        score.updated_at = utcnow()
        session.flush()
