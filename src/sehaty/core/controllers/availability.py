"""Doctor availability business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A doctor
manages the recurring weekly windows from which concrete bookable slots are
derived (see ``sehaty.core.services.slots``). Failures raise the ``SehatyError``
taxonomy; methods never return ``None`` to signal an error.
"""

from datetime import time

from sehaty.db import Availability, DoctorProfile
from sqlalchemy import delete, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError


class AvailabilityRow(DomainModel):
    """One recurring weekly availability window (detached projection).

    The plain, id-based view returned by :meth:`AvailabilityController.add` /
    :meth:`list` — the transport layer serialises it directly.
    """

    id: int
    weekday: int
    start_time: time
    end_time: time
    slot_minutes: int


class AvailabilityController:
    @staticmethod
    def add(
        doctor_id: int,
        weekday: int,
        start_time: time,
        end_time: time,
        slot_minutes: int = 30,
    ) -> AvailabilityRow:
        """Create a recurring weekly availability window for a doctor.

        Validates ``weekday`` in 0..6 (Mon..Sun) and ``start_time < end_time``;
        a positive ``slot_minutes``. Returns the created (detached) row.
        """
        if not 0 <= weekday <= 6:
            raise SehatyValidationError("weekday must be between 0 (Monday) and 6 (Sunday)")
        if start_time >= end_time:
            raise SehatyValidationError("start_time must be before end_time")
        if slot_minutes <= 0:
            raise SehatyValidationError("slot_minutes must be positive")
        with get_session() as session:
            avail = Availability(
                doctor_id=doctor_id,
                weekday=weekday,
                start_time=start_time,
                end_time=end_time,
                slot_minutes=slot_minutes,
            )
            session.add(avail)
            session.flush()
            return AvailabilityRow.model_validate(avail)

    @staticmethod
    def update(
        doctor_id: int,
        availability_id: int,
        weekday: int,
        start_time: time,
        end_time: time,
        slot_minutes: int = 30,
    ) -> AvailabilityRow:
        """Adjust a recurring weekly window the doctor owns (in place).

        Same validation as :meth:`add`. Raises ``SehatyNotFoundError`` if the row
        does not exist or belongs to a different doctor.
        """
        if not 0 <= weekday <= 6:
            raise SehatyValidationError("weekday must be between 0 (Monday) and 6 (Sunday)")
        if start_time >= end_time:
            raise SehatyValidationError("start_time must be before end_time")
        if slot_minutes <= 0:
            raise SehatyValidationError("slot_minutes must be positive")
        with get_session() as session:
            avail = session.get(Availability, availability_id)
            if avail is None or avail.doctor_id != doctor_id:
                raise SehatyNotFoundError(
                    f"no availability {availability_id} for doctor {doctor_id}"
                )
            avail.weekday = weekday
            avail.start_time = start_time
            avail.end_time = end_time
            avail.slot_minutes = slot_minutes
            session.flush()
            return AvailabilityRow.model_validate(avail)

    @staticmethod
    def list(doctor_id: int) -> list[AvailabilityRow]:
        """Return a doctor's availability windows, ordered by weekday then time."""
        stmt = (
            select(Availability)
            .where(Availability.doctor_id == doctor_id)
            .order_by(Availability.weekday, Availability.start_time)
        )
        with get_session() as session:
            return [
                AvailabilityRow.model_validate(a) for a in session.execute(stmt).scalars().all()
            ]

    @staticmethod
    def delete(doctor_id: int, availability_id: int) -> None:
        """Delete an availability window the doctor owns.

        Raises ``SehatyNotFoundError`` if the row does not exist or belongs to a
        different doctor (ownership check).
        """
        with get_session() as session:
            avail = session.get(Availability, availability_id)
            if avail is None or avail.doctor_id != doctor_id:
                raise SehatyNotFoundError(
                    f"no availability {availability_id} for doctor {doctor_id}"
                )
            session.delete(avail)


def mirror_opening_hours(doctor_id: int, *, slot_minutes: int = 30) -> int:
    """Turn the cabinet's published opening hours into a bookable agenda.

    These are two different things that look like one thing, and the difference
    has bitten every onboarding rehearsal. ``opening_hours`` on the profile is
    what the public page *displays*; ``availabilities`` is what actually
    generates slots. Fill the first at a visit and the doctor's agenda stays
    empty — they have bought a booking engine that offers nothing, and nobody
    finds out until a patient tries.

    In practice a doctor states one set of hours and means both. So this copies
    the published hours across, which is the answer they would have given
    anyway. Returns how many windows were created.

    Replaces the existing recurring schedule rather than adding to it: running
    it twice must not double every window, and an operator correcting hours in
    front of the doctor expects the correction to stick, not to accumulate.
    """
    with get_session() as session:
        profile = session.get(DoctorProfile, doctor_id)
        if profile is None:
            raise SehatyNotFoundError(f"no doctor profile for user {doctor_id}")

        hours = profile.opening_hours or []
        session.execute(delete(Availability).where(Availability.doctor_id == doctor_id))

        created = 0
        for entry in hours:
            weekday = entry.get("weekday")
            if weekday is None or not 0 <= int(weekday) <= 6:
                continue
            for span in entry.get("ranges") or []:
                if len(span) != 2:
                    continue
                start, end = (time.fromisoformat(str(s)) for s in span)
                if start >= end:
                    continue
                session.add(
                    Availability(
                        doctor_id=doctor_id,
                        weekday=int(weekday),
                        start_time=start,
                        end_time=end,
                        slot_minutes=slot_minutes,
                    )
                )
                created += 1
        session.flush()
        return created
