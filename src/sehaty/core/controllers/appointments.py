"""Appointment lifecycle business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). Patients
book concrete slots (validated against ``sehaty.core.services.slots``); doctors
and patients then drive the appointment through a constrained status matrix.
Every booking and transition writes an immutable ``AuditLog`` entry. Failures
raise the ``SehatyError`` taxonomy; methods never return ``None`` for an error.
"""

from datetime import UTC, datetime

from sehaty.db import Appointment, AppointmentStatus, AuditLog, UserRole
from sqlalchemy import select

from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyForbiddenError,
)
from sehaty.core.services.slots import find_slot_end

# Allowed status transitions per role. A transition is legal iff the target
# status is in the set keyed by the appointment's current status.
DOCTOR_TRANSITIONS: dict[AppointmentStatus, set[AppointmentStatus]] = {
    AppointmentStatus.REQUESTED: {AppointmentStatus.CONFIRMED, AppointmentStatus.CANCELLED},
    AppointmentStatus.CONFIRMED: {
        AppointmentStatus.COMPLETED,
        AppointmentStatus.CANCELLED,
        AppointmentStatus.NO_SHOW,
    },
}
PATIENT_TRANSITIONS: dict[AppointmentStatus, set[AppointmentStatus]] = {
    AppointmentStatus.REQUESTED: {AppointmentStatus.CANCELLED},
    AppointmentStatus.CONFIRMED: {AppointmentStatus.CANCELLED},
}


class AppointmentController:
    @staticmethod
    def book(
        patient_id: int,
        doctor_id: int,
        start_at: datetime,
        reason: str | None = None,
    ) -> Appointment:
        """Book a free slot for a patient and record a ``BOOK`` audit entry.

        Validates ``start_at`` is a genuine free slot for the doctor; raises
        ``SehatyConflictError`` if it is already booked or outside availability.
        Creates the appointment as ``REQUESTED`` and returns it (detached).
        """
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=UTC)
        with get_session() as session:
            end_at = find_slot_end(session, doctor_id, start_at)
            if end_at is None:
                raise SehatyConflictError(
                    "requested slot is not available (already booked or outside availability)"
                )
            appt = Appointment(
                patient_id=patient_id,
                doctor_id=doctor_id,
                start_at=start_at,
                end_at=end_at,
                status=AppointmentStatus.REQUESTED,
                reason=reason,
            )
            session.add(appt)
            session.flush()
            session.add(
                AuditLog(
                    actor_user_id=patient_id,
                    action="BOOK",
                    entity="appointment",
                    entity_id=appt.id,
                )
            )
            session.flush()

        # Notify the doctor of the new request AFTER the booking commits (its own
        # session, never nested). A notification failure must NEVER break the
        # booking — the appointment is already persisted above.
        try:
            from sehaty.core.controllers.notifications import NotificationController

            NotificationController.notify(
                doctor_id,
                kind="appointment_booked",
                message="New appointment requested",
                entity="appointment",
                entity_id=appt.id,
            )
        except Exception:
            pass
        return appt

    @staticmethod
    def list_for(user_id: int, role: UserRole) -> list[Appointment]:
        """List a user's appointments, ordered by start time.

        A patient sees the appointments they booked; a doctor sees the ones on
        their calendar. Any other role sees none.
        """
        stmt = select(Appointment)
        if role == UserRole.PATIENT:
            stmt = stmt.where(Appointment.patient_id == user_id)
        elif role == UserRole.DOCTOR:
            stmt = stmt.where(Appointment.doctor_id == user_id)
        else:
            return []
        stmt = stmt.order_by(Appointment.start_at)
        with get_session() as session:
            return list(session.execute(stmt).scalars().all())

    @staticmethod
    def transition(
        user_id: int,
        role: UserRole,
        appointment_id: int,
        new_status: AppointmentStatus,
        notes: str | None = None,
    ) -> Appointment:
        """Move an appointment to ``new_status`` under the role-based matrix.

        Only the owning doctor or patient may act (else ``SehatyForbiddenError``)
        and only along a legal edge of the transition matrix (else
        ``SehatyConflictError``). Records an ``APPT_<STATUS>`` audit entry.
        """
        with get_session() as session:
            appt = session.get(Appointment, appointment_id)
            if appt is None:
                raise SehatyForbiddenError("not your appointment")

            if role == UserRole.DOCTOR and appt.doctor_id == user_id:
                allowed = DOCTOR_TRANSITIONS.get(appt.status, set())
            elif role == UserRole.PATIENT and appt.patient_id == user_id:
                allowed = PATIENT_TRANSITIONS.get(appt.status, set())
            else:
                raise SehatyForbiddenError("not your appointment")

            if new_status not in allowed:
                raise SehatyConflictError(
                    f"cannot move from {appt.status.value} to {new_status.value}"
                )

            appt.status = new_status
            if notes is not None:
                appt.notes = notes
            session.add(
                AuditLog(
                    actor_user_id=user_id,
                    action=f"APPT_{new_status.value}",
                    entity="appointment",
                    entity_id=appt.id,
                )
            )
            session.flush()
            patient_id = appt.patient_id

        # Notify the patient of the new status AFTER the transition commits (its
        # own session, never nested). Only a legal transition reaches here — a
        # raised transition never emits. A notification failure must NEVER break
        # the transition, which is already persisted above.
        try:
            from sehaty.core.controllers.notifications import NotificationController

            human = new_status.value.lower().replace("_", " ")
            NotificationController.notify(
                patient_id,
                kind=f"appointment_{new_status.value.lower()}",
                message=f"Your appointment is now {human}",
                entity="appointment",
                entity_id=appointment_id,
            )
        except Exception:
            pass
        return appt
