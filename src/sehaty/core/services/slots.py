"""Slot generation: expand weekly availabilities into concrete bookable slots.

Times are handled in UTC. ``Availability`` defines recurring weekly windows;
slots are the concrete bookable ``(start, end)`` datetimes derived from them,
minus what is already booked by an active appointment.

Ported from the prototype's tz-aware logic
(``backend/app/services/appointments.py``).
"""

from datetime import UTC, date, datetime, time, timedelta

from sehaty.db import Appointment, AppointmentStatus, Availability
from sqlalchemy import select
from sqlalchemy.orm import Session

MAX_RANGE_DAYS = 31

# Statuses that still occupy a slot (a cancelled/no-show slot frees up again).
ACTIVE_STATUSES = (AppointmentStatus.REQUESTED, AppointmentStatus.CONFIRMED)


def _combine(day: date, t: time) -> datetime:
    """Combine a calendar day and a wall-clock time into a UTC-aware datetime."""
    return datetime.combine(day, t, tzinfo=UTC)


def _as_utc(dt: datetime) -> datetime:
    """Normalise a (possibly naive) datetime to UTC-aware."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _booked_starts(
    session: Session, doctor_id: int, from_dt: datetime, to_dt: datetime
) -> set[datetime]:
    """Return the UTC start times of active appointments in ``[from_dt, to_dt)``."""
    rows = session.execute(
        select(Appointment.start_at).where(
            Appointment.doctor_id == doctor_id,
            Appointment.status.in_(ACTIVE_STATUSES),
            Appointment.start_at >= from_dt,
            Appointment.start_at < to_dt,
        )
    ).scalars()
    return {_as_utc(s) for s in rows}


def available_slots(
    session: Session, doctor_id: int, from_date: date, to_date: date
) -> list[tuple[datetime, datetime]]:
    """Return the free ``(start, end)`` slots for a doctor across a date range.

    Weekly availabilities are expanded into concrete UTC slots across the
    inclusive ``[from_date, to_date]`` range (capped at ``MAX_RANGE_DAYS``),
    minus the starts already taken by active (REQUESTED/CONFIRMED) appointments.
    """
    if (to_date - from_date).days > MAX_RANGE_DAYS:
        to_date = from_date + timedelta(days=MAX_RANGE_DAYS)

    availabilities = (
        session.execute(select(Availability).where(Availability.doctor_id == doctor_id))
        .scalars()
        .all()
    )
    if not availabilities:
        return []

    by_weekday: dict[int, list[Availability]] = {}
    for a in availabilities:
        by_weekday.setdefault(a.weekday, []).append(a)

    range_start = _combine(from_date, time.min)
    range_end = _combine(to_date + timedelta(days=1), time.min)
    booked = _booked_starts(session, doctor_id, range_start, range_end)

    slots: list[tuple[datetime, datetime]] = []
    day = from_date
    while day <= to_date:
        for a in by_weekday.get(day.weekday(), []):
            cursor = _combine(day, a.start_time)
            end_of_window = _combine(day, a.end_time)
            step = timedelta(minutes=a.slot_minutes)
            while cursor + step <= end_of_window:
                if cursor not in booked:
                    slots.append((cursor, cursor + step))
                cursor += step
        day += timedelta(days=1)

    slots.sort(key=lambda s: s[0])
    return slots


def find_slot_end(session: Session, doctor_id: int, start_at: datetime) -> datetime | None:
    """Validate a requested start against real availability; return its end.

    Returns the slot's end datetime if ``start_at`` is a genuine free slot for
    the doctor, otherwise ``None`` (outside availability or already booked).
    """
    start_at = _as_utc(start_at)
    day = start_at.date()
    for start, end in available_slots(session, doctor_id, day, day):
        if start == start_at:
            return end
    return None
