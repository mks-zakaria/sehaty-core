"""Landing-page analytics: record interactions, roll them up per doctor.

The rollup is what turns a free page into a paid subscription: three months in,
a doctor is shown their own numbers — "340 views, 22 taps on Appeler, 0 became
appointments, because you are not on the booking system". That argument needs
real data from day one, which is why ingestion exists before any dashboard does.

Writes are deliberately cheap and forgiving: an event is telemetry, not a
transaction. A malformed or unattributable event is dropped rather than raised,
because a beacon from a patient's phone must never turn into a visible error on
a doctor's public page.
"""

from datetime import UTC, datetime, timedelta

from sehaty.db import (
    DoctorProfile,
    LandingEvent,
    LandingEventType,
    User,
    VerificationStatus,
)
from sqlalchemy import func, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyValidationError

# Only coarse buckets are stored; anything else is normalized to "other" so a
# caller cannot smuggle a search query (and thus health data) into the column.
_KNOWN_SOURCES = {"qr", "google", "direct", "whatsapp", "facebook", "instagram", "other"}
_MAX_WINDOW_DAYS = 366


class EventCounts(DomainModel):
    """One doctor's landing activity over a window."""

    page_views: int
    qr_scans: int
    call_clicks: int
    whatsapp_clicks: int
    directions_clicks: int
    book_clicks: int

    @property
    def total_intent(self) -> int:
        """Taps that signal a patient actually trying to reach the cabinet."""
        return self.call_clicks + self.whatsapp_clicks + self.book_clicks


class LandingAnalyticsController:
    @staticmethod
    def record(*, doctor_slug: str, event_type: str, source: str | None = None) -> bool:
        """Record one interaction. Returns False when it was dropped.

        Dropped rather than raised for an unknown slug, an unpublished doctor or
        an unrecognized event type: this is called from a fire-and-forget beacon
        on a public page, and a 500 there would surface as a broken page to a
        patient while doing nothing useful for anyone.
        """
        try:
            parsed = LandingEventType(event_type)
        except ValueError:
            return False

        normalized = (source or "").strip().lower() or None
        if normalized is not None and normalized not in _KNOWN_SOURCES:
            normalized = "other"

        with get_session() as session:
            doctor_id = session.execute(
                select(DoctorProfile.user_id)
                .join(User, User.id == DoctorProfile.user_id)
                .where(
                    DoctorProfile.slug == doctor_slug,
                    User.is_active.is_(True),
                    DoctorProfile.verification_status.in_(VerificationStatus.publicly_visible()),
                )
            ).scalar_one_or_none()
            if doctor_id is None:
                return False

            session.add(LandingEvent(doctor_id=doctor_id, type=parsed, source=normalized))
        return True

    @staticmethod
    def counts_for_doctor(doctor_id: int, *, days: int = 30) -> EventCounts:
        """Aggregate one doctor's events over the last ``days``.

        This is the monthly report a doctor is shown. ``days`` is bounded so a
        caller cannot ask for an unindexed full-table scan.
        """
        if days <= 0 or days > _MAX_WINDOW_DAYS:
            raise SehatyValidationError(f"days out of range: {days}")

        since = datetime.now(UTC) - timedelta(days=days)
        stmt = (
            select(LandingEvent.type, func.count().label("n"))
            .where(LandingEvent.doctor_id == doctor_id, LandingEvent.occurred_at >= since)
            .group_by(LandingEvent.type)
        )
        with get_session() as session:
            rows = session.execute(stmt).all()

        by_type = {row.type: int(row.n) for row in rows}
        return EventCounts(
            page_views=by_type.get(LandingEventType.PAGE_VIEW, 0),
            qr_scans=by_type.get(LandingEventType.QR_SCAN, 0),
            call_clicks=by_type.get(LandingEventType.CALL_CLICK, 0),
            whatsapp_clicks=by_type.get(LandingEventType.WHATSAPP_CLICK, 0),
            directions_clicks=by_type.get(LandingEventType.DIRECTIONS_CLICK, 0),
            book_clicks=by_type.get(LandingEventType.BOOK_CLICK, 0),
        )
