"""Waitlist: turn a freed slot back into a booked one.

Releasing a slot is worthless unless something fills it. This is the other half
of the confirmation system, and the half that makes the subscription defensible:
a doctor pays 199 DH/month because empty chairs stop being empty, not because a
screen turned a row red.

Ordering is strictly first-come. Anything cleverer (rank by no-show history,
by fee, by loyalty) is unexplainable to the patient who was skipped and
indefensible when they ask — and they do ask.

Offers are time-boxed. An offer with no deadline lets one slow reply block the
queue while the slot sits empty, which is the exact failure the feature exists
to prevent.
"""

from datetime import UTC, datetime, timedelta

from sehaty.db import (
    Appointment,
    AppointmentStatus,
    DoctorProfile,
    PatientProfile,
    User,
    WaitlistEntry,
    WaitlistStatus,
)
from sqlalchemy import or_, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyNotFoundError,
    SehatyValidationError,
)
from sehaty.core.services.entitlement import booking_enabled

# How long a patient has to take an offered slot before it moves down the queue.
OFFER_TTL_MINUTES = 120


class WaitlistRow(DomainModel):
    """One waiting patient, as the secretary sees the queue."""

    entry_id: int
    patient_id: int
    patient_name: str
    patient_phone: str | None
    status: str
    joined_at: datetime
    earliest_at: datetime | None
    latest_at: datetime | None
    note: str | None


class OfferResult(DomainModel):
    """Outcome of offering a freed slot to the queue."""

    slot_appointment_id: int
    offered_to: WaitlistRow | None
    # False when nobody was eligible — the slot is free but stays unfilled.
    offered: bool


class PatientWaitlistRow(DomainModel):
    """One of the caller's own waitlist entries, with the offer if there is one."""

    entry_id: int
    doctor_id: int
    doctor_name: str | None
    doctor_slug: str | None
    status: str
    joined_at: datetime
    # Set only while a slot is on the table. The patient cannot act without it,
    # and without a read like this an offer is invisible to the person it is for.
    offered_start_at: datetime | None
    offered_end_at: datetime | None
    offer_expires_at: datetime | None


class WaitlistController:
    @staticmethod
    def join(
        doctor_id: int,
        patient_id: int,
        *,
        earliest_at: datetime | None = None,
        latest_at: datetime | None = None,
        note: str | None = None,
    ) -> int:
        """Put a patient on a doctor's waitlist. Returns the entry id.

        Re-joining reactivates the existing row rather than creating a second
        one — the unique constraint enforces it, and duplicate entries would
        quietly double someone's odds.
        """
        if earliest_at and latest_at and earliest_at >= latest_at:
            raise SehatyValidationError("earliest_at must precede latest_at")
        # A doctor whose subscription lapsed serves no slots at all, so a queue
        # for them is a queue that can never move. Refusing here beats letting
        # someone wait indefinitely for an offer the system cannot make.
        if not booking_enabled(doctor_id):
            raise SehatyValidationError("this doctor is not taking online bookings")

        with get_session() as session:
            existing = session.execute(
                select(WaitlistEntry).where(
                    WaitlistEntry.doctor_id == doctor_id,
                    WaitlistEntry.patient_id == patient_id,
                )
            ).scalar_one_or_none()

            if existing is not None:
                if existing.status == WaitlistStatus.WAITING:
                    raise SehatyConflictError("already on this doctor's waitlist")
                # Rejoining after passing or cancelling goes to the back of the
                # queue: `joined_at` is refreshed, so nobody keeps an old
                # position by leaving and returning.
                existing.status = WaitlistStatus.WAITING
                existing.joined_at = datetime.now(UTC)
                existing.earliest_at = earliest_at
                existing.latest_at = latest_at
                existing.note = note
                existing.offered_appointment_id = None
                existing.offered_at = None
                session.flush()
                return int(existing.id)

            entry = WaitlistEntry(
                doctor_id=doctor_id,
                patient_id=patient_id,
                status=WaitlistStatus.WAITING,
                earliest_at=earliest_at,
                latest_at=latest_at,
                note=note,
            )
            session.add(entry)
            session.flush()
            return int(entry.id)

    @staticmethod
    def for_patient(patient_id: int, *, now: datetime | None = None) -> list[PatientWaitlistRow]:
        """The caller's own waitlist entries, newest offer first.

        Without this an offered slot is invisible to the patient it was offered
        to: the doctor's screen shows the offer went out, and the patient has
        nowhere to see it. Offers are time-boxed, so the deadline is returned
        alongside — a countdown the patient cannot see is not a deadline.
        """
        now = now or datetime.now(UTC)

        with get_session() as session:
            rows = session.execute(
                select(
                    WaitlistEntry.id,
                    WaitlistEntry.doctor_id,
                    WaitlistEntry.status,
                    WaitlistEntry.joined_at,
                    WaitlistEntry.offered_at,
                    DoctorProfile.full_name,
                    DoctorProfile.slug,
                    Appointment.start_at,
                    Appointment.end_at,
                )
                # Outer: a patient's own entry must never disappear from their
                # list because the doctor row is missing. Better a nameless row
                # they can still decline than a silently vanished offer.
                .outerjoin(DoctorProfile, DoctorProfile.user_id == WaitlistEntry.doctor_id)
                .outerjoin(Appointment, Appointment.id == WaitlistEntry.offered_appointment_id)
                .where(
                    WaitlistEntry.patient_id == patient_id,
                    WaitlistEntry.status.in_([WaitlistStatus.WAITING, WaitlistStatus.OFFERED]),
                )
                .order_by(WaitlistEntry.offered_at.desc().nullslast())
            ).all()

        out: list[PatientWaitlistRow] = []
        for row in rows:
            offered_at = _as_utc(row.offered_at)
            out.append(
                PatientWaitlistRow(
                    entry_id=row.id,
                    doctor_id=row.doctor_id,
                    doctor_name=row.full_name,
                    doctor_slug=row.slug,
                    status=str(row.status),
                    joined_at=row.joined_at,
                    offered_start_at=row.start_at,
                    offered_end_at=row.end_at,
                    offer_expires_at=(
                        offered_at + timedelta(minutes=OFFER_TTL_MINUTES) if offered_at else None
                    ),
                )
            )
        return out

    @staticmethod
    def leave(entry_id: int, patient_id: int) -> None:
        """Remove a patient from a waitlist (their own entry only)."""
        with get_session() as session:
            entry = session.get(WaitlistEntry, entry_id)
            if entry is None or entry.patient_id != patient_id:
                raise SehatyNotFoundError(f"no waitlist entry {entry_id}")
            entry.status = WaitlistStatus.CANCELLED

    @staticmethod
    def queue(doctor_id: int) -> list[WaitlistRow]:
        """The doctor's live queue, oldest first."""
        with get_session() as session:
            rows = session.execute(
                select(
                    WaitlistEntry.id,
                    WaitlistEntry.patient_id,
                    WaitlistEntry.status,
                    WaitlistEntry.joined_at,
                    WaitlistEntry.earliest_at,
                    WaitlistEntry.latest_at,
                    WaitlistEntry.note,
                    PatientProfile.full_name,
                    User.phone,
                )
                .join(User, User.id == WaitlistEntry.patient_id)
                .outerjoin(PatientProfile, PatientProfile.user_id == WaitlistEntry.patient_id)
                .where(
                    WaitlistEntry.doctor_id == doctor_id,
                    WaitlistEntry.status.in_([WaitlistStatus.WAITING, WaitlistStatus.OFFERED]),
                )
                .order_by(WaitlistEntry.joined_at.asc())
            ).all()

        return [
            WaitlistRow(
                entry_id=row.id,
                patient_id=row.patient_id,
                patient_name=row.full_name or "Patient",
                patient_phone=row.phone,
                status=str(row.status),
                joined_at=row.joined_at,
                earliest_at=row.earliest_at,
                latest_at=row.latest_at,
                note=row.note,
            )
            for row in rows
        ]

    @staticmethod
    def release_slot(appointment_id: int, *, now: datetime | None = None) -> OfferResult:
        """Free an appointment's slot and offer it to the next eligible patient.

        This is the action behind the secretary's "Libérer le créneau" button on
        a red-flagged row. Cancelling and offering are one operation on purpose:
        a slot freed but never offered is the failure this feature exists to
        prevent, and two buttons means the second one gets forgotten.
        """
        now = now or datetime.now(UTC)

        with get_session() as session:
            appointment = session.get(Appointment, appointment_id)
            if appointment is None:
                raise SehatyNotFoundError(f"no appointment {appointment_id}")

            appointment.status = AppointmentStatus.CANCELLED

            # Expire stale offers first, so a patient who never answered stops
            # holding up the queue.
            _expire_offers(session, appointment.doctor_id, now)

            candidate = session.execute(
                select(WaitlistEntry)
                .where(
                    WaitlistEntry.doctor_id == appointment.doctor_id,
                    WaitlistEntry.status == WaitlistStatus.WAITING,
                    WaitlistEntry.patient_id != appointment.patient_id,
                    or_(
                        WaitlistEntry.earliest_at.is_(None),
                        WaitlistEntry.earliest_at <= appointment.start_at,
                    ),
                    or_(
                        WaitlistEntry.latest_at.is_(None),
                        WaitlistEntry.latest_at >= appointment.start_at,
                    ),
                )
                .order_by(WaitlistEntry.joined_at.asc())
                .limit(1)
            ).scalar_one_or_none()

            if candidate is None:
                return OfferResult(
                    slot_appointment_id=appointment_id, offered_to=None, offered=False
                )

            candidate.status = WaitlistStatus.OFFERED
            candidate.offered_appointment_id = appointment_id
            candidate.offered_at = now

            profile = session.get(PatientProfile, candidate.patient_id)
            user = session.get(User, candidate.patient_id)
            row = WaitlistRow(
                entry_id=int(candidate.id),
                patient_id=candidate.patient_id,
                patient_name=(profile.full_name if profile else None) or "Patient",
                patient_phone=user.phone if user else None,
                status=str(candidate.status),
                joined_at=candidate.joined_at,
                earliest_at=candidate.earliest_at,
                latest_at=candidate.latest_at,
                note=candidate.note,
            )
            return OfferResult(slot_appointment_id=appointment_id, offered_to=row, offered=True)

    @staticmethod
    def accept_offer(entry_id: int, patient_id: int) -> int:
        """Take an offered slot. Returns the appointment id now held.

        The freed appointment is reassigned rather than duplicated, so the slot
        cannot be double-booked by an accept racing a walk-in.
        """
        with get_session() as session:
            entry = session.get(WaitlistEntry, entry_id)
            if entry is None or entry.patient_id != patient_id:
                raise SehatyNotFoundError(f"no waitlist entry {entry_id}")
            if entry.status != WaitlistStatus.OFFERED or not entry.offered_appointment_id:
                raise SehatyConflictError("no slot is currently offered to you")

            appointment = session.get(Appointment, entry.offered_appointment_id)
            if appointment is None:
                raise SehatyNotFoundError("the offered slot no longer exists")
            if appointment.status != AppointmentStatus.CANCELLED:
                # Someone already took it.
                entry.status = WaitlistStatus.PASSED
                raise SehatyConflictError("that slot has already been taken")

            appointment.patient_id = patient_id
            appointment.status = AppointmentStatus.CONFIRMED
            # A patient who actively claimed a slot has confirmed by definition.
            appointment.clinic_patient_id = None
            entry.status = WaitlistStatus.ACCEPTED
            return int(appointment.id)

    @staticmethod
    def decline_offer(entry_id: int, patient_id: int) -> None:
        """Turn down an offered slot; the patient stays on the list."""
        with get_session() as session:
            entry = session.get(WaitlistEntry, entry_id)
            if entry is None or entry.patient_id != patient_id:
                raise SehatyNotFoundError(f"no waitlist entry {entry_id}")
            entry.status = WaitlistStatus.WAITING
            entry.offered_appointment_id = None
            entry.offered_at = None

    @staticmethod
    def expire_stale_offers(*, now: datetime | None = None) -> int:
        """Return unanswered offers to the queue. Returns how many expired."""
        now = now or datetime.now(UTC)
        with get_session() as session:
            return _expire_offers(session, None, now)


def _as_utc(value: datetime | None) -> datetime | None:
    """Postgres returns aware datetimes, SQLite naive; normalize before arithmetic."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _expire_offers(session, doctor_id: int | None, now: datetime) -> int:  # noqa: ANN001
    """Move timed-out offers back to WAITING so the queue keeps moving."""
    cutoff = now - timedelta(minutes=OFFER_TTL_MINUTES)
    stmt = select(WaitlistEntry).where(
        WaitlistEntry.status == WaitlistStatus.OFFERED,
        WaitlistEntry.offered_at.is_not(None),
        WaitlistEntry.offered_at <= cutoff,
    )
    if doctor_id is not None:
        stmt = stmt.where(WaitlistEntry.doctor_id == doctor_id)

    entries = session.execute(stmt).scalars().all()
    for entry in entries:
        entry.status = WaitlistStatus.WAITING
        entry.offered_appointment_id = None
        entry.offered_at = None
    return len(entries)
