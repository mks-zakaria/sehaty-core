"""Appointment lifecycle business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). Patients
book concrete slots (validated against ``sehaty.core.services.slots``); doctors
and patients then drive the appointment through a constrained status matrix.
Every booking and transition writes an immutable ``AuditLog`` entry. Failures
raise the ``SehatyError`` taxonomy; methods never return ``None`` for an error.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sehaty.db import (
    Appointment,
    AppointmentStatus,
    AuditLog,
    ClinicPatient,
    DoctorProfile,
    User,
    UserRole,
)
from sqlalchemy import select

from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyForbiddenError,
)
from sehaty.core.services.slots import find_slot_end


@dataclass(frozen=True)
class AppointmentGridRow:
    """One appointment in a doctor's booking-slot grid (detached projection).

    ``patient_name`` is always human-readable: it never surfaces a bare id when
    any contact detail exists (see ``AppointmentController.list_for_doctor``).
    """

    id: int
    clinic_patient_id: int | None
    patient_name: str
    patient_phone: str | None
    start_at: datetime
    end_at: datetime
    status: str
    reason: str | None


@dataclass(frozen=True)
class PatientAppointmentRow:
    """One appointment in a patient's own list (detached projection).

    ``doctor_name`` is always human-readable: it resolves from the doctor's
    :class:`DoctorProfile` ``full_name`` and falls back to ``"Doctor #{id}"``
    when the doctor has no profile (or an empty name).
    """

    id: int
    doctor_id: int
    doctor_name: str
    start_at: datetime
    end_at: datetime
    status: str
    reason: str | None


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

            # Auto-link the booking to the doctor's patient register (same session,
            # no nesting). Reuse the doctor's existing register row for this app
            # patient, or create one — backfilling contact details from the patient
            # User (full_name stays NULL; patients carry no stored name).
            register = session.execute(
                select(ClinicPatient).where(
                    ClinicPatient.doctor_id == doctor_id,
                    ClinicPatient.user_id == patient_id,
                )
            ).scalar_one_or_none()
            if register is None:
                contact = session.execute(
                    select(User.phone, User.email).where(User.id == patient_id)
                ).one_or_none()
                register = ClinicPatient(
                    doctor_id=doctor_id,
                    user_id=patient_id,
                    phone=contact.phone if contact is not None else None,
                    email=contact.email if contact is not None else None,
                    created_by=patient_id,
                )
                session.add(register)
                session.flush()

            appt = Appointment(
                patient_id=patient_id,
                doctor_id=doctor_id,
                start_at=start_at,
                end_at=end_at,
                status=AppointmentStatus.REQUESTED,
                reason=reason,
                clinic_patient_id=register.id,
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
    def list_for_doctor(
        doctor_id: int,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list["AppointmentGridRow"]:
        """List a doctor's appointments with a human-readable patient name.

        Powers the booking-slot grid. One column-only ``select(...)`` left-joins
        each appointment to the doctor's patient register (:class:`ClinicPatient`
        on ``clinic_patient_id``) AND to the booking :class:`User` (on
        ``patient_id``), selecting only scalar columns — no PostGIS ``geopoint`` —
        so it runs on SQLite (tests) and Postgres alike.

        ``patient_name`` resolves down a fallback chain so the grid always shows
        something human: the linked register row's ``full_name``, else its
        ``email`` / ``phone``, else the booking user's ``email`` / ``phone``, else
        ``"Patient #{patient_id}"``. ``patient_phone`` prefers the register row's
        phone, then the user's.

        ``date_from`` / ``date_to`` (datetimes), when given, filter ``start_at``
        into the half-open ``[date_from, date_to)`` interval. Ordered by
        ``start_at`` (then ``id`` for a stable tie-break).
        """
        stmt = (
            select(
                Appointment.id,
                Appointment.clinic_patient_id,
                Appointment.patient_id,
                Appointment.start_at,
                Appointment.end_at,
                Appointment.status,
                Appointment.reason,
                ClinicPatient.full_name.label("cp_full_name"),
                ClinicPatient.email.label("cp_email"),
                ClinicPatient.phone.label("cp_phone"),
                User.email.label("user_email"),
                User.phone.label("user_phone"),
            )
            .outerjoin(ClinicPatient, Appointment.clinic_patient_id == ClinicPatient.id)
            .outerjoin(User, Appointment.patient_id == User.id)
            .where(Appointment.doctor_id == doctor_id)
        )
        if date_from is not None:
            stmt = stmt.where(Appointment.start_at >= date_from)
        if date_to is not None:
            stmt = stmt.where(Appointment.start_at < date_to)
        stmt = stmt.order_by(Appointment.start_at, Appointment.id)

        with get_session() as session:
            rows = session.execute(stmt).all()

        grid: list[AppointmentGridRow] = []
        for row in rows:
            name = (
                row.cp_full_name
                or row.cp_email
                or row.cp_phone
                or row.user_email
                or row.user_phone
                or f"Patient #{row.patient_id}"
            )
            grid.append(
                AppointmentGridRow(
                    id=row.id,
                    clinic_patient_id=row.clinic_patient_id,
                    patient_name=name,
                    patient_phone=row.cp_phone or row.user_phone,
                    start_at=row.start_at,
                    end_at=row.end_at,
                    status=str(row.status),
                    reason=row.reason,
                )
            )
        return grid

    @staticmethod
    def list_for_patient_view(patient_user_id: int) -> list["PatientAppointmentRow"]:
        """List a patient's own appointments with a human-readable doctor name.

        One column-only ``select(...)`` left-joins each appointment to the
        doctor's :class:`DoctorProfile` (on ``DoctorProfile.user_id ==
        Appointment.doctor_id``), selecting only scalar columns — no PostGIS
        ``geopoint`` — so it runs on SQLite (tests) and Postgres alike.

        ``doctor_name`` resolves to the profile's ``full_name``, falling back to
        ``"Doctor #{doctor_id}"`` when the doctor has no profile or an empty
        name. Ordered by ``start_at`` (then ``id`` for a stable tie-break).
        """
        stmt = (
            select(
                Appointment.id,
                Appointment.doctor_id,
                Appointment.start_at,
                Appointment.end_at,
                Appointment.status,
                Appointment.reason,
                DoctorProfile.full_name.label("doctor_full_name"),
            )
            .outerjoin(DoctorProfile, DoctorProfile.user_id == Appointment.doctor_id)
            .where(Appointment.patient_id == patient_user_id)
            .order_by(Appointment.start_at, Appointment.id)
        )

        with get_session() as session:
            rows = session.execute(stmt).all()

        result: list[PatientAppointmentRow] = []
        for row in rows:
            name = row.doctor_full_name or f"Doctor #{row.doctor_id}"
            result.append(
                PatientAppointmentRow(
                    id=row.id,
                    doctor_id=row.doctor_id,
                    doctor_name=name,
                    start_at=row.start_at,
                    end_at=row.end_at,
                    status=str(row.status),
                    reason=row.reason,
                )
            )
        return result

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
