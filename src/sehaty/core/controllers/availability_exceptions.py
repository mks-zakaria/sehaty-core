"""Doctor availability-exception business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A doctor
overrides their recurring weekly schedule on specific dates: ``BLOCK`` closes a
day (or a time range within it), ``OPEN`` adds a one-off window, and ``CAP``
limits that date to ``max_patients`` bookings. These feed
``sehaty.core.services.slots``. Failures raise the ``SehatyError`` taxonomy;
methods never return ``None`` to signal an error.
"""

from datetime import date, time

from sehaty.db import AvailabilityException, AvailabilityExceptionKind
from sqlalchemy import select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError


class ExceptionRow(DomainModel):
    """One availability exception (detached projection)."""

    id: int
    date: date
    kind: str
    start_time: time | None
    end_time: time | None
    slot_minutes: int | None
    max_patients: int | None
    reason: str | None


def _row(exc: AvailabilityException) -> ExceptionRow:
    return ExceptionRow(
        id=exc.id,
        date=exc.date,
        kind=str(exc.kind),
        start_time=exc.start_time,
        end_time=exc.end_time,
        slot_minutes=exc.slot_minutes,
        max_patients=exc.max_patients,
        reason=exc.reason,
    )


def _validate(
    kind: AvailabilityExceptionKind | str,
    start_time: time | None,
    end_time: time | None,
    slot_minutes: int | None,
    max_patients: int | None,
) -> tuple[AvailabilityExceptionKind, time | None, time | None, int | None, int | None]:
    """Validate an exception and return normalised (kind, start, end, slot, cap).

    Fields irrelevant to the kind are cleared so a BLOCK never carries a cap and
    a CAP never carries a window.
    """
    try:
        kind = AvailabilityExceptionKind(kind)
    except ValueError as exc:
        raise SehatyValidationError("kind must be one of BLOCK, OPEN, CAP") from exc

    if kind == AvailabilityExceptionKind.OPEN:
        if start_time is None or end_time is None:
            raise SehatyValidationError("OPEN requires start_time and end_time")
        if start_time >= end_time:
            raise SehatyValidationError("start_time must be before end_time")
        if not slot_minutes or slot_minutes <= 0:
            raise SehatyValidationError("OPEN requires a positive slot_minutes")
        return kind, start_time, end_time, slot_minutes, None

    if kind == AvailabilityExceptionKind.CAP:
        if not max_patients or max_patients <= 0:
            raise SehatyValidationError("CAP requires a positive max_patients")
        return kind, None, None, None, max_patients

    # BLOCK
    if start_time is not None or end_time is not None:
        # A timed BLOCK needs both bounds, well-ordered.
        if start_time is None or end_time is None:
            raise SehatyValidationError("a timed BLOCK requires both start_time and end_time")
        if start_time >= end_time:
            raise SehatyValidationError("start_time must be before end_time")
    return kind, start_time, end_time, None, None


class AvailabilityExceptionController:
    @staticmethod
    def add(
        doctor_id: int,
        date: date,
        kind: AvailabilityExceptionKind | str,
        start_time: time | None = None,
        end_time: time | None = None,
        slot_minutes: int | None = None,
        reason: str | None = None,
        max_patients: int | None = None,
    ) -> ExceptionRow:
        """Create a date-specific exception for a doctor.

        ``kind`` must be ``BLOCK``, ``OPEN``, or ``CAP``. An ``OPEN`` requires a
        window (``start_time < end_time``) and a positive ``slot_minutes``. A
        *timed* ``BLOCK`` (either bound given) requires ``start_time < end_time``;
        a ``BLOCK`` with neither bound closes the whole day. A ``CAP`` requires a
        positive ``max_patients`` and ignores the window. Returns the created
        (detached) :class:`ExceptionRow`.
        """
        kind, start_time, end_time, slot_minutes, max_patients = _validate(
            kind, start_time, end_time, slot_minutes, max_patients
        )
        with get_session() as session:
            exc = AvailabilityException(
                doctor_id=doctor_id,
                date=date,
                kind=kind,
                start_time=start_time,
                end_time=end_time,
                slot_minutes=slot_minutes,
                max_patients=max_patients,
                reason=reason,
            )
            session.add(exc)
            session.flush()
            return _row(exc)

    @staticmethod
    def update(
        doctor_id: int,
        exception_id: int,
        date: date,
        kind: AvailabilityExceptionKind | str,
        start_time: time | None = None,
        end_time: time | None = None,
        slot_minutes: int | None = None,
        reason: str | None = None,
        max_patients: int | None = None,
    ) -> ExceptionRow:
        """Replace an exception the doctor owns with new (validated) values.

        Same validation as :meth:`add`. Raises ``SehatyNotFoundError`` if the row
        does not exist or belongs to a different doctor.
        """
        kind, start_time, end_time, slot_minutes, max_patients = _validate(
            kind, start_time, end_time, slot_minutes, max_patients
        )
        with get_session() as session:
            exc = session.get(AvailabilityException, exception_id)
            if exc is None or exc.doctor_id != doctor_id:
                raise SehatyNotFoundError(
                    f"no availability exception {exception_id} for doctor {doctor_id}"
                )
            exc.date = date
            exc.kind = kind
            exc.start_time = start_time
            exc.end_time = end_time
            exc.slot_minutes = slot_minutes
            exc.max_patients = max_patients
            exc.reason = reason
            session.flush()
            return _row(exc)

    @staticmethod
    def list(
        doctor_id: int,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[ExceptionRow]:
        """Return a doctor's exceptions, ordered by date then id.

        ``date_from`` / ``date_to`` (inclusive) narrow the range when given.
        """
        stmt = select(AvailabilityException).where(AvailabilityException.doctor_id == doctor_id)
        if date_from is not None:
            stmt = stmt.where(AvailabilityException.date >= date_from)
        if date_to is not None:
            stmt = stmt.where(AvailabilityException.date <= date_to)
        stmt = stmt.order_by(AvailabilityException.date, AvailabilityException.id)
        with get_session() as session:
            return [_row(e) for e in session.execute(stmt).scalars().all()]

    @staticmethod
    def delete(doctor_id: int, exception_id: int) -> None:
        """Delete an exception the doctor owns.

        Raises ``SehatyNotFoundError`` if the row does not exist or belongs to a
        different doctor (ownership check).
        """
        with get_session() as session:
            exc = session.get(AvailabilityException, exception_id)
            if exc is None or exc.doctor_id != doctor_id:
                raise SehatyNotFoundError(
                    f"no availability exception {exception_id} for doctor {doctor_id}"
                )
            session.delete(exc)
