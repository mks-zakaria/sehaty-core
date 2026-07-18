"""Doctor practice-insights analytics (read-only, doctor-scoped).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A doctor's
practice page wants a trend view rather than a single at-a-glance number: how did
the last few months look — how many appointments, how many completed vs no-show
vs cancelled, roughly how much cash that represents, how bad is the no-show
problem, and how is the review reputation trending.
:meth:`DoctorAnalyticsController.doctor_analytics` answers all of that over a
rolling window of the last ``months`` calendar months.

Everything here is read-only and doctor-scoped: every query filters by
``doctor_id``. Reads return small frozen dataclass projections (never ORM
objects), so callers hold detached, serialisable data. Every ``select(...)`` is
column-only — never the PostGIS ``geopoint`` — so the same query runs on the
SQLite test engine and on Postgres alike; bucketing is done in Python from a
column-only row set rather than with dialect-specific date functions.

Two documented simplifications:

* **Month bucketing is UTC.** Appointments and reviews are bucketed by the UTC
  year-month of their timestamp, not the clinic's local timezone
  (``DoctorProfile.timezone``). A per-clinic tz-perfect bucketing is a
  nice-to-have; the UTC calendar month is simple, portable, and good enough for
  a trend summary — and it is documented as such.
* **Revenue is an ESTIMATE.** ``estimated_revenue`` multiplies the count of
  COMPLETED appointments in a month by the doctor's ``consultation_fee`` (0 when
  unset). Cash is collected at the desk and is not platform-processed, so this is
  a modelled estimate of gross consultation revenue, not booked cash — the
  accounting trail in :mod:`sehaty.core.controllers.reporting` is the source of
  truth for actual collections.
"""

from datetime import UTC, datetime

from sehaty.db import (
    Appointment,
    AppointmentStatus,
    DoctorProfile,
    ReputationScore,
    Review,
    ReviewDirection,
    ReviewStatus,
)
from sqlalchemy import select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session


class MonthStat(DomainModel):
    """One calendar month of appointment activity for a doctor (detached).

    ``month`` is the UTC year-month key ``"YYYY-MM"``. ``total`` counts every
    appointment whose ``start_at`` falls in the month (any status); ``completed``
    / ``no_show`` / ``cancelled`` are the per-status sub-counts.
    ``estimated_revenue`` is ``completed * consultation_fee`` — a modelled
    estimate, not booked cash (see module docstring).
    """

    month: str
    total: int
    completed: int
    no_show: int
    cancelled: int
    estimated_revenue: float


class MonthCount(DomainModel):
    """A published-review count for one UTC calendar month (detached)."""

    month: str
    count: int


class DoctorAnalytics(DomainModel):
    """Practice-insights trend for one doctor over a rolling window (detached).

    * ``by_month`` — one :class:`MonthStat` per month in the window, oldest to
      newest (always exactly ``months`` entries, zero-filled for quiet months).
    * ``no_show_rate`` — ``no_show / (completed + no_show)`` over the whole
      window; ``0.0`` when a doctor has neither (no completed-or-no-show visits).
    * ``total_appointments`` / ``total_completed`` / ``total_no_show`` — window
      totals across every bucket.
    * ``avg_rating`` / ``review_count`` — the doctor's materialized
      :class:`ReputationScore` (``0`` / ``0.0`` when they have none yet).
    * ``reviews_by_month`` — count of PUBLISHED PATIENT_ON_DOCTOR reviews
      targeting the doctor, bucketed by ``created_at`` UTC year-month over the
      same window (always exactly ``months`` entries, zero-filled).
    """

    by_month: list[MonthStat]
    no_show_rate: float
    total_appointments: int
    total_completed: int
    total_no_show: int
    avg_rating: float
    review_count: int
    reviews_by_month: list[MonthCount]


def _month_key(dt: datetime) -> str:
    """UTC year-month key ``"YYYY-MM"`` for a (tz-aware or naive-UTC) timestamp."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return f"{dt.year:04d}-{dt.month:02d}"


def _window_months(now: datetime, months: int) -> list[str]:
    """The last ``months`` UTC year-month keys ending at ``now``, oldest→newest."""
    year, month = now.year, now.month
    keys: list[str] = []
    # Walk backwards from the current month, then reverse to oldest→newest.
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    keys.reverse()
    return keys


class DoctorAnalyticsController:
    @staticmethod
    def doctor_analytics(
        doctor_id: int, months: int = 6, now: datetime | None = None
    ) -> DoctorAnalytics:
        """Return the doctor's practice-insights trend over the last ``months``.

        ``now`` defaults to the current UTC instant and anchors the rolling
        window; the window is the ``months`` calendar months ending with the
        month of ``now`` (inclusive), oldest→newest. Appointments and reviews are
        bucketed in Python by their UTC year-month from a column-only row set (no
        PostGIS ``geopoint``, no dialect-specific date functions) so the same code
        runs on SQLite and Postgres.

        ``estimated_revenue`` per month is ``completed_that_month *
        consultation_fee`` (the doctor's fee, ``0`` when unset) — an estimate, not
        booked cash (see module docstring). ``no_show_rate`` is
        ``no_show / (completed + no_show)`` over the window, ``0.0`` when that
        denominator is zero. ``avg_rating`` / ``review_count`` come straight from
        the materialized :class:`ReputationScore`.
        """
        if now is None:
            now = datetime.now(UTC)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)

        window = _window_months(now, months)
        window_set = set(window)
        # Oldest bucket's first instant — a cheap SQL lower bound so we scan only
        # rows that can land in the window (Python still owns the exact bucketing).
        oldest_year, oldest_month = int(window[0][:4]), int(window[0][5:7])
        lower_bound = datetime(oldest_year, oldest_month, 1, tzinfo=UTC)

        # Per-month appointment sub-counts, keyed by year-month.
        appt_buckets: dict[str, dict[str, int]] = {
            key: {"total": 0, "completed": 0, "no_show": 0, "cancelled": 0} for key in window
        }
        review_buckets: dict[str, int] = {key: 0 for key in window}

        with get_session() as session:
            # Doctor's consultation fee for the revenue estimate (0 when unset).
            fee = session.execute(
                select(DoctorProfile.consultation_fee).where(DoctorProfile.user_id == doctor_id)
            ).scalar_one_or_none()
            consultation_fee = float(fee) if fee is not None else 0.0

            # Column-only projection of this doctor's appointments in range; bucket
            # in Python so no dialect-specific date grouping is needed.
            appt_rows = session.execute(
                select(Appointment.start_at, Appointment.status).where(
                    Appointment.doctor_id == doctor_id,
                    Appointment.start_at >= lower_bound,
                )
            ).all()
            for start_at, status in appt_rows:
                key = _month_key(start_at)
                if key not in window_set:
                    continue
                bucket = appt_buckets[key]
                bucket["total"] += 1
                if status == AppointmentStatus.COMPLETED:
                    bucket["completed"] += 1
                elif status == AppointmentStatus.NO_SHOW:
                    bucket["no_show"] += 1
                elif status == AppointmentStatus.CANCELLED:
                    bucket["cancelled"] += 1

            # Published patient-on-doctor reviews targeting this doctor, in range.
            review_rows = session.execute(
                select(Review.created_at).where(
                    Review.target_id == doctor_id,
                    Review.direction == ReviewDirection.PATIENT_ON_DOCTOR,
                    Review.status == ReviewStatus.PUBLISHED,
                    Review.created_at >= lower_bound,
                )
            ).all()
            for (created_at,) in review_rows:
                key = _month_key(created_at)
                if key in window_set:
                    review_buckets[key] += 1

            # Materialized reputation (0 when the doctor has no score row yet).
            score = session.get(ReputationScore, doctor_id)
            avg_rating = float(score.avg_stars) if score is not None else 0.0
            review_count = int(score.review_count) if score is not None else 0

        by_month = [
            MonthStat(
                month=key,
                total=appt_buckets[key]["total"],
                completed=appt_buckets[key]["completed"],
                no_show=appt_buckets[key]["no_show"],
                cancelled=appt_buckets[key]["cancelled"],
                estimated_revenue=appt_buckets[key]["completed"] * consultation_fee,
            )
            for key in window
        ]
        reviews_by_month = [MonthCount(month=key, count=review_buckets[key]) for key in window]

        total_appointments = sum(m.total for m in by_month)
        total_completed = sum(m.completed for m in by_month)
        total_no_show = sum(m.no_show for m in by_month)
        denom = total_completed + total_no_show
        no_show_rate = (total_no_show / denom) if denom else 0.0

        return DoctorAnalytics(
            by_month=by_month,
            no_show_rate=no_show_rate,
            total_appointments=total_appointments,
            total_completed=total_completed,
            total_no_show=total_no_show,
            avg_rating=avg_rating,
            review_count=review_count,
            reviews_by_month=reviews_by_month,
        )
