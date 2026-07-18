"""Doctor home-page dashboard stats (read-only).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A doctor's
home page wants a few at-a-glance numbers — how busy is today, how many requests
are waiting to be confirmed, what's coming up this week, how big is the register,
and what's the very next patient walking in. :meth:`DashboardController.doctor_stats`
answers all of that in a handful of small aggregate queries plus one "next" query.

Everything here is read-only and doctor-scoped: every query filters by
``doctor_id``. Reads return small frozen dataclass projections (never ORM
objects), so callers hold detached, serialisable data. Every ``select(...)`` is
column-only — no PostGIS ``geopoint`` — so the same query runs on the SQLite test
engine and on Postgres alike.

"Today" is the current UTC calendar date. A per-clinic timezone-perfect "today"
(using ``DoctorProfile.timezone``) is a nice-to-have, but the UTC calendar date is
simple, portable, and good enough for a home-page summary; it is documented as such.
"""

from datetime import UTC, datetime, timedelta

from sehaty.db import Appointment, AppointmentStatus, ClinicPatient, User
from sqlalchemy import case, func, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session

# Statuses that count as a live, upcoming appointment (awaiting or confirmed).
_UPCOMING = (AppointmentStatus.REQUESTED, AppointmentStatus.CONFIRMED)


class NextAppointment(DomainModel):
    """The doctor's soonest upcoming appointment (detached projection).

    ``patient_name`` resolves like ``AppointmentController.list_for_doctor``: the
    linked register row's ``full_name``, else its ``email`` / ``phone``, else the
    booking user's ``email`` / ``phone``, else ``"Patient #{patient_id}"``.
    """

    start_at: datetime
    status: str
    patient_name: str


class DoctorDashboard(DomainModel):
    """At-a-glance home-page stats for one doctor (detached projection).

    * ``today_total`` — appointments whose ``start_at`` falls on today's UTC
      calendar date, any status.
    * ``today_confirmed`` — of today's, the ones that are CONFIRMED.
    * ``to_confirm`` — REQUESTED appointments with ``start_at >= now`` (upcoming
      requests awaiting the doctor's confirmation).
    * ``upcoming_7d`` — REQUESTED/CONFIRMED appointments with ``start_at`` in the
      half-open window ``[now, now + 7d)``.
    * ``patients_total`` — number of register rows (:class:`ClinicPatient`) the
      doctor owns.
    * ``next_appointment`` — the soonest REQUESTED/CONFIRMED appointment with
      ``start_at >= now``, or ``None`` when the doctor has nothing coming up.
    """

    today_total: int
    today_confirmed: int
    to_confirm: int
    upcoming_7d: int
    patients_total: int
    next_appointment: NextAppointment | None


class DashboardController:
    @staticmethod
    def doctor_stats(doctor_id: int, now: datetime | None = None) -> DoctorDashboard:
        """Return the doctor's home-page dashboard stats (read-only).

        ``now`` defaults to the current UTC instant and is normalised to UTC-aware
        so the window comparisons compare cleanly against the tz-aware ``start_at``
        column. "Today" is the UTC calendar date of ``now`` (see module docstring).

        Computes the today totals in one grouped-by-status pass over today's
        ``[day_start, day_end)`` window, the ``to_confirm`` / ``upcoming_7d`` counts
        and ``patients_total`` as small independent ``func.count()`` queries, and
        the ``next_appointment`` as one ordered "soonest upcoming" query left-joined
        to the register + booking user for a human-readable name. Every projection
        is column-only (no PostGIS ``geopoint``) so it runs on SQLite and Postgres.
        """
        if now is None:
            now = datetime.now(UTC)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)

        # Today's UTC calendar-day window, half-open [day_start, day_end).
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        week_end = now + timedelta(days=7)

        with get_session() as session:
            # Today's totals in one pass: count all of today's appointments and,
            # conditionally, the CONFIRMED ones (portable case-sum, avoids the
            # non-portable .filter() aggregate).
            today_row = session.execute(
                select(
                    func.count(Appointment.id),
                    func.sum(case((Appointment.status == AppointmentStatus.CONFIRMED, 1), else_=0)),
                ).where(
                    Appointment.doctor_id == doctor_id,
                    Appointment.start_at >= day_start,
                    Appointment.start_at < day_end,
                )
            ).one()
            today_total = int(today_row[0] or 0)
            today_confirmed = int(today_row[1] or 0)

            # Upcoming REQUESTED requests awaiting confirmation.
            to_confirm = session.execute(
                select(func.count(Appointment.id)).where(
                    Appointment.doctor_id == doctor_id,
                    Appointment.status == AppointmentStatus.REQUESTED,
                    Appointment.start_at >= now,
                )
            ).scalar_one()

            # Live upcoming appointments in the next 7 days.
            upcoming_7d = session.execute(
                select(func.count(Appointment.id)).where(
                    Appointment.doctor_id == doctor_id,
                    Appointment.status.in_(_UPCOMING),
                    Appointment.start_at >= now,
                    Appointment.start_at < week_end,
                )
            ).scalar_one()

            # Register size.
            patients_total = session.execute(
                select(func.count(ClinicPatient.id)).where(ClinicPatient.doctor_id == doctor_id)
            ).scalar_one()

            # Soonest upcoming appointment, with a human-readable patient name.
            # Column-only left-joins to the register + booking user (no geopoint).
            next_row = session.execute(
                select(
                    Appointment.patient_id,
                    Appointment.start_at,
                    Appointment.status,
                    ClinicPatient.full_name.label("cp_full_name"),
                    ClinicPatient.email.label("cp_email"),
                    ClinicPatient.phone.label("cp_phone"),
                    User.email.label("user_email"),
                    User.phone.label("user_phone"),
                )
                .outerjoin(ClinicPatient, Appointment.clinic_patient_id == ClinicPatient.id)
                .outerjoin(User, Appointment.patient_id == User.id)
                .where(
                    Appointment.doctor_id == doctor_id,
                    Appointment.status.in_(_UPCOMING),
                    Appointment.start_at >= now,
                )
                .order_by(Appointment.start_at, Appointment.id)
                .limit(1)
            ).one_or_none()

        next_appointment: NextAppointment | None = None
        if next_row is not None:
            name = (
                next_row.cp_full_name
                or next_row.cp_email
                or next_row.cp_phone
                or next_row.user_email
                or next_row.user_phone
                or f"Patient #{next_row.patient_id}"
            )
            next_appointment = NextAppointment(
                start_at=next_row.start_at,
                status=str(next_row.status),
                patient_name=name,
            )

        return DoctorDashboard(
            today_total=today_total,
            today_confirmed=today_confirmed,
            to_confirm=int(to_confirm),
            upcoming_7d=int(upcoming_7d),
            patients_total=int(patients_total),
            next_appointment=next_appointment,
        )
