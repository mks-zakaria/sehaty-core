"""Appointment lifecycle business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). Patients
book concrete slots (validated against ``sehaty.core.services.slots``); doctors
and patients then drive the appointment through a constrained status matrix.
Every booking and transition writes an immutable ``AuditLog`` entry. Failures
raise the ``SehatyError`` taxonomy; methods never return ``None`` for an error.
"""

from datetime import UTC, datetime, timedelta

from pydantic import Field
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    AuditLog,
    CabinetSession,
    ClinicPatient,
    DoctorProfile,
    User,
    UserRole,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyForbiddenError,
    SehatyNotFoundError,
)
from sehaty.core.services.slots import _as_utc, daily_cap_reached, find_slot_end


class AppointmentGridRow(DomainModel):
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
    status: AppointmentStatus
    reason: str | None


class PatientAppointmentRow(DomainModel):
    """One appointment in a patient's own list (detached projection).

    ``doctor_name`` is always human-readable: it resolves from the doctor's
    :class:`DoctorProfile` ``full_name`` and falls back to ``"Doctor #{id}"``
    when the doctor has no profile (or an empty name).
    """

    id: int
    doctor_id: int
    doctor_name: str
    doctor_slug: str | None
    start_at: datetime
    end_at: datetime
    status: AppointmentStatus
    reason: str | None


class AppointmentRow(DomainModel):
    """A single appointment as seen by its patient or doctor (detached projection).

    The plain, id-based view (no resolved names) returned by
    :meth:`AppointmentController.book` / :meth:`list_for` / :meth:`transition` /
    :meth:`reschedule` — the transport layer serialises it directly.
    """

    id: int
    patient_id: int
    doctor_id: int
    # Carried for callers (e.g. register-linking checks) but never serialised —
    # the wire contract never exposed the doctor's patient-register row id.
    clinic_patient_id: int | None = Field(default=None, exclude=True)
    start_at: datetime
    end_at: datetime
    status: AppointmentStatus
    reason: str | None
    notes: str | None


class ConsultationRow(DomainModel):
    """The consultation (encounter) record on an appointment — the LLM-training unit.

    Returned by :meth:`AppointmentController.start_consultation` /
    :meth:`complete_consultation`: the real start/finish times plus the structured
    clinical data the doctor recorded at the desk. Diagnoses and prescriptions are
    separate rows that link back to this appointment.
    """

    id: int
    status: AppointmentStatus
    cabinet_session_id: int | None
    consultation_started_at: datetime | None
    consultation_ended_at: datetime | None
    chief_complaint: str | None
    symptoms: dict | None
    vitals: dict | None
    exam_notes: str | None


class WaitingPatientRow(DomainModel):
    """One checked-in patient in a doctor's waiting queue, with the profile to verify.

    Powers the doctor's "next patient" screen: the register patient's human details
    (name, phone, sex, birth year, prior no-shows) alongside the appointment they're
    checked in for.
    """

    appointment_id: int
    clinic_patient_id: int | None
    patient_name: str
    patient_phone: str | None
    sex: str | None
    birth_year: int | None
    no_show_count: int
    start_at: datetime
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


def _load_for_acting_doctor(
    session: Session, appointment_id: int, doctor_id: int
) -> Appointment:
    """Load an appointment and assert ``doctor_id`` is its session's acting doctor.

    Raises ``SehatyNotFoundError`` if the appointment is gone, ``SehatyConflictError``
    if it was never checked in to a cabinet session, and ``SehatyForbiddenError`` if
    the caller is not the doctor covering that session.
    """
    appt = session.get(Appointment, appointment_id)
    if appt is None:
        raise SehatyNotFoundError(f"appointment {appointment_id} not found")
    if appt.cabinet_session_id is None:
        raise SehatyConflictError("appointment is not checked in to a cabinet session")
    cabinet_session = session.get(CabinetSession, appt.cabinet_session_id)
    if cabinet_session is None or cabinet_session.acting_doctor_id != doctor_id:
        raise SehatyForbiddenError("not the acting doctor for this appointment")
    return appt


class AppointmentController:
    @staticmethod
    def book(
        patient_id: int,
        doctor_id: int,
        start_at: datetime,
        reason: str | None = None,
    ) -> AppointmentRow:
        """Book a free slot for a patient and record a ``BOOK`` audit entry.

        Validates ``start_at`` is a genuine free slot for the doctor; raises
        ``SehatyConflictError`` if it is already booked or outside availability.
        Creates the appointment as ``REQUESTED`` and returns it (detached).
        """
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=UTC)
        with get_session() as session:
            # Cap first: a full day suppresses its slots, so checking the cap
            # before find_slot_end lets the clear "day is full" message win over
            # the generic "slot not available" one.
            if daily_cap_reached(session, doctor_id, start_at):
                raise SehatyConflictError("the doctor is fully booked for that day")
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
            # Race safety net. The find_slot_end pre-check above is the fast path,
            # but two concurrent bookings of the same free slot both pass it and
            # only collide at flush: Postgres' ``appointments_no_overlap`` EXCLUDE
            # constraint then rejects the second insert with an IntegrityError
            # naming ``appointments_no_overlap``. Catch it and surface a clean 409
            # rather than a raw driver error. Any IntegrityError on this insert
            # means the slot is no longer free, so we do not need to inspect the
            # message to know the booking lost the race.
            try:
                session.flush()
            except IntegrityError as exc:
                raise SehatyConflictError("that slot was just taken") from exc
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
        return AppointmentRow.model_validate(appt)

    @staticmethod
    def list_for(user_id: int, role: UserRole) -> list[AppointmentRow]:
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
            return [AppointmentRow.model_validate(a) for a in session.execute(stmt).scalars().all()]

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
                    status=row.status,
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
        name. ``doctor_slug`` carries the profile's ``slug`` (``None`` when the
        doctor has no profile) so the patient app can deep-link to the doctor and
        load reschedule slots. Ordered by ``start_at`` (then ``id`` for a stable
        tie-break).
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
                DoctorProfile.slug.label("doctor_slug"),
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
                    doctor_slug=row.doctor_slug,
                    start_at=row.start_at,
                    end_at=row.end_at,
                    status=row.status,
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
    ) -> AppointmentRow:
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
        return AppointmentRow.model_validate(appt)

    @staticmethod
    def reschedule(
        user_id: int,
        role: UserRole,
        appointment_id: int,
        new_start_at: datetime,
        notes: str | None = None,
    ) -> AppointmentRow:
        """Move an existing appointment to a different free slot.

        A first-class alternative to cancel + rebook: the same appointment row is
        moved, preserving its identity, ``patient_id``/``doctor_id`` link, and
        register association. Ownership follows :meth:`transition`: a PATIENT must
        own ``patient_id``, a DOCTOR must own ``doctor_id`` (else
        ``SehatyForbiddenError``); a linked assistant is served by the API passing
        the acting doctor's id with role ``DOCTOR``, so there is no
        assistant-specific branch here.

        Only a REQUESTED or CONFIRMED appointment may move (a COMPLETED /
        CANCELLED / NO_SHOW one raises ``SehatyConflictError``). ``new_start_at``
        must be a genuine FREE slot for the doctor, validated tz-aware via
        :func:`find_slot_end`; an unavailable slot (already booked or outside
        availability) raises ``SehatyConflictError``. Passing the appointment's
        *current* start is a no-op success — the row is returned unchanged (this
        avoids the slot check rejecting the appointment's own — necessarily
        "booked" — slot, and spares a spurious audit/notification).

        Status semantics: a PATIENT move resets the status to REQUESTED (a moved
        appointment needs the doctor's re-confirmation); a DOCTOR move preserves
        the current status. ``notes`` when provided is stored on the row. Writes
        an ``APPT_RESCHEDULED`` audit entry, and AFTER commit notifies the OTHER
        party (kind ``appointment_rescheduled``) — non-fatal, mirroring
        :meth:`book` / :meth:`transition`. Returns the updated (detached)
        appointment.
        """
        if new_start_at.tzinfo is None:
            new_start_at = new_start_at.replace(tzinfo=UTC)
        else:
            new_start_at = new_start_at.astimezone(UTC)

        with get_session() as session:
            appt = session.get(Appointment, appointment_id)
            if appt is None:
                raise SehatyForbiddenError("not your appointment")

            if role == UserRole.DOCTOR and appt.doctor_id == user_id:
                pass
            elif role == UserRole.PATIENT and appt.patient_id == user_id:
                pass
            else:
                raise SehatyForbiddenError("not your appointment")

            if appt.status not in (AppointmentStatus.REQUESTED, AppointmentStatus.CONFIRMED):
                raise SehatyConflictError(f"cannot reschedule a {appt.status.value} appointment")

            doctor_id = appt.doctor_id
            patient_id = appt.patient_id

            # Moving to the slot it already occupies is a no-op success. Short-
            # circuit BEFORE find_slot_end, which would otherwise reject the
            # appointment's own (self-booked, hence not "free") slot.
            if appt.start_at is not None and _as_utc(appt.start_at) == new_start_at:
                if notes is not None:
                    appt.notes = notes
                    session.flush()
                return AppointmentRow.model_validate(appt)

            # Cap first (excluding the appointment being moved), so a full target
            # day surfaces the clear "day is full" message before find_slot_end.
            if daily_cap_reached(
                session, doctor_id, new_start_at, exclude_appointment_id=appt.id
            ):
                raise SehatyConflictError("the doctor is fully booked for that day")
            new_end_at = find_slot_end(session, doctor_id, new_start_at)
            if new_end_at is None:
                raise SehatyConflictError("requested slot is not available")

            appt.start_at = new_start_at
            appt.end_at = new_end_at
            if role == UserRole.PATIENT:
                appt.status = AppointmentStatus.REQUESTED
            if notes is not None:
                appt.notes = notes

            # Race safety net, identical in spirit to book(): the find_slot_end
            # pre-check is the fast path, but two concurrent moves onto the same
            # free slot both pass it and only collide at flush against the
            # ``appointments_no_overlap`` EXCLUDE constraint (Postgres). Surface a
            # clean 409 rather than a raw driver error.
            try:
                session.flush()
            except IntegrityError as exc:
                raise SehatyConflictError("that slot was just taken") from exc

            session.add(
                AuditLog(
                    actor_user_id=user_id,
                    action="APPT_RESCHEDULED",
                    entity="appointment",
                    entity_id=appt.id,
                )
            )
            session.flush()

        # Notify the OTHER party AFTER the move commits (its own session, never
        # nested). A patient's move alerts the doctor; a doctor's move alerts the
        # patient. A notification failure must NEVER break the reschedule, which
        # is already persisted above.
        recipient = doctor_id if role == UserRole.PATIENT else patient_id
        try:
            from sehaty.core.controllers.notifications import NotificationController

            NotificationController.notify(
                recipient,
                kind="appointment_rescheduled",
                message="An appointment was rescheduled",
                entity="appointment",
                entity_id=appointment_id,
            )
        except Exception:
            pass
        return AppointmentRow.model_validate(appt)

    @staticmethod
    def check_in(appointment_id: int, cabinet_session_id: int) -> AppointmentRow:
        """Secretary checks a patient in against an open cabinet session.

        Moves a REQUESTED/CONFIRMED appointment to CHECKED_IN, links it to the
        session (whose acting doctor may be a substitute covering for the owner),
        and notifies that doctor their next patient is ready. Raises if the
        appointment cannot be checked in, or the session is missing/closed.
        """
        with get_session() as session:
            appt = session.get(Appointment, appointment_id)
            if appt is None:
                raise SehatyNotFoundError(f"appointment {appointment_id} not found")
            if appt.status not in (AppointmentStatus.REQUESTED, AppointmentStatus.CONFIRMED):
                raise SehatyConflictError(f"cannot check in a {appt.status.value} appointment")
            cabinet_session = session.get(CabinetSession, cabinet_session_id)
            if cabinet_session is None:
                raise SehatyNotFoundError(f"cabinet session {cabinet_session_id} not found")
            if not cabinet_session.is_open:
                raise SehatyConflictError("cabinet session is closed")

            appt.status = AppointmentStatus.CHECKED_IN
            appt.cabinet_session_id = cabinet_session_id
            acting_doctor_id = cabinet_session.acting_doctor_id
            session.add(
                AuditLog(
                    actor_user_id=acting_doctor_id,
                    action="APPT_CHECKED_IN",
                    entity="appointment",
                    entity_id=appt.id,
                )
            )
            patient_name = None
            if appt.clinic_patient_id is not None:
                patient_name = session.execute(
                    select(ClinicPatient.full_name).where(
                        ClinicPatient.id == appt.clinic_patient_id
                    )
                ).scalar_one_or_none()
            patient_name = patient_name or f"Patient #{appt.patient_id}"
            session.flush()
            result = AppointmentRow.model_validate(appt)

        # Notify the acting doctor AFTER commit (own session, never nested). A
        # notification failure must never break the check-in.
        try:
            from sehaty.core.controllers.notifications import NotificationController

            NotificationController.notify(
                acting_doctor_id,
                kind="next_patient",
                message=f"Next patient checked in: {patient_name}",
                entity="appointment",
                entity_id=appointment_id,
            )
        except Exception:
            pass
        return result

    @staticmethod
    def start_consultation(
        appointment_id: int, doctor_id: int, now: datetime | None = None
    ) -> ConsultationRow:
        """The acting doctor starts the consultation (CHECKED_IN -> IN_PROGRESS)."""
        now = datetime.now(UTC) if now is None else _as_utc(now)
        with get_session() as session:
            appt = _load_for_acting_doctor(session, appointment_id, doctor_id)
            if appt.status != AppointmentStatus.CHECKED_IN:
                raise SehatyConflictError(f"cannot start a {appt.status.value} appointment")
            appt.status = AppointmentStatus.IN_PROGRESS
            appt.consultation_started_at = now
            session.add(
                AuditLog(
                    actor_user_id=doctor_id,
                    action="APPT_IN_PROGRESS",
                    entity="appointment",
                    entity_id=appt.id,
                )
            )
            session.flush()
            return ConsultationRow.model_validate(appt)

    @staticmethod
    def complete_consultation(
        appointment_id: int,
        doctor_id: int,
        *,
        chief_complaint: str | None = None,
        symptoms: dict | None = None,
        vitals: dict | None = None,
        exam_notes: str | None = None,
        now: datetime | None = None,
    ) -> ConsultationRow:
        """The acting doctor finishes and records the consultation (IN_PROGRESS -> COMPLETED).

        Stamps ``consultation_ended_at`` and stores the structured clinical data.
        Diagnoses and prescriptions are separate rows that link to this appointment
        and may be recorded any time before completion.
        """
        now = datetime.now(UTC) if now is None else _as_utc(now)
        with get_session() as session:
            appt = _load_for_acting_doctor(session, appointment_id, doctor_id)
            if appt.status != AppointmentStatus.IN_PROGRESS:
                raise SehatyConflictError(f"cannot complete a {appt.status.value} appointment")
            appt.status = AppointmentStatus.COMPLETED
            appt.consultation_ended_at = now
            if chief_complaint is not None:
                appt.chief_complaint = chief_complaint
            if symptoms is not None:
                appt.symptoms = symptoms
            if vitals is not None:
                appt.vitals = vitals
            if exam_notes is not None:
                appt.exam_notes = exam_notes
            session.add(
                AuditLog(
                    actor_user_id=doctor_id,
                    action="APPT_COMPLETED",
                    entity="appointment",
                    entity_id=appt.id,
                )
            )
            session.flush()
            return ConsultationRow.model_validate(appt)

    @staticmethod
    def waiting_queue(doctor_id: int) -> list[WaitingPatientRow]:
        """The acting doctor's live queue: CHECKED_IN patients with their profile."""
        stmt = (
            select(
                Appointment.id,
                Appointment.clinic_patient_id,
                Appointment.start_at,
                Appointment.reason,
                ClinicPatient.full_name,
                ClinicPatient.phone,
                ClinicPatient.sex,
                ClinicPatient.birth_year,
                ClinicPatient.no_show_count,
            )
            .join(CabinetSession, Appointment.cabinet_session_id == CabinetSession.id)
            .outerjoin(ClinicPatient, Appointment.clinic_patient_id == ClinicPatient.id)
            .where(
                Appointment.status == AppointmentStatus.CHECKED_IN,
                CabinetSession.acting_doctor_id == doctor_id,
            )
            .order_by(Appointment.start_at, Appointment.id)
        )
        with get_session() as session:
            rows = session.execute(stmt).all()
        return [
            WaitingPatientRow(
                appointment_id=r.id,
                clinic_patient_id=r.clinic_patient_id,
                patient_name=r.full_name or f"Patient #{r.id}",
                patient_phone=r.phone,
                sex=r.sex,
                birth_year=r.birth_year,
                no_show_count=r.no_show_count or 0,
                start_at=r.start_at,
                reason=r.reason,
            )
            for r in rows
        ]

    @staticmethod
    def run_reminders(within_hours: int = 24, now: datetime | None = None) -> int:
        """Send a one-time patient reminder for each imminent CONFIRMED appointment.

        Scans CONFIRMED appointments whose ``start_at`` falls in the half-open
        window ``[now, now + within_hours)`` and that have not yet been reminded
        (``reminder_sent_at IS NULL``). For every match it stamps
        ``reminder_sent_at = now`` and commits FIRST, then emits a patient
        notification (kind ``appointment_reminder``) per appointment AFTER the
        commit — the same never-nested, non-fatal notify pattern as
        :meth:`book` / :meth:`transition`.

        Stamping the marker before notifying makes the reminder idempotent: a
        second run finds those rows non-NULL and skips them, so a reminder is
        never sent twice even across repeated (e.g. cron) invocations. A single
        failed notify neither re-notifies on the next run nor blocks the other
        reminders in this batch. Returns the number of appointments reminded on
        THIS call.

        ``now`` defaults to the current UTC instant and is normalised to
        UTC-aware so the window compares cleanly against the tz-aware
        ``start_at`` column.
        """
        if now is None:
            now = datetime.now(UTC)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
        window_end = now + timedelta(hours=within_hours)

        # Collect the due appointments and mark them reminded INSIDE the session
        # (column-only projection — no PostGIS geopoint — so it runs on SQLite
        # and Postgres alike). Marking before we notify is what dedupes repeated
        # runs.
        with get_session() as session:
            rows = session.execute(
                select(
                    Appointment.id,
                    Appointment.patient_id,
                    Appointment.doctor_id,
                    Appointment.start_at,
                ).where(
                    Appointment.status == AppointmentStatus.CONFIRMED,
                    Appointment.reminder_sent_at.is_(None),
                    Appointment.start_at >= now,
                    Appointment.start_at < window_end,
                )
            ).all()
            pending = [(r.id, r.patient_id, r.doctor_id, r.start_at) for r in rows]
            if not pending:
                return 0

            session.execute(
                update(Appointment)
                .where(Appointment.id.in_([appt_id for appt_id, *_ in pending]))
                .values(reminder_sent_at=now)
            )

        # Best-effort doctor display names for the message (its own session,
        # column-only — no PostGIS geopoint). This is purely decorative: the
        # marker is already committed, so any failure here just falls back to
        # "your doctor" and never un-marks or blocks a reminder.
        doctor_names: dict[int, str] = {}
        try:
            doctor_ids = {doctor_id for _, _, doctor_id, _ in pending}
            with get_session() as session:
                for user_id, full_name in session.execute(
                    select(DoctorProfile.user_id, DoctorProfile.full_name).where(
                        DoctorProfile.user_id.in_(doctor_ids)
                    )
                ).all():
                    if full_name:
                        doctor_names[user_id] = full_name
        except Exception:
            doctor_names = {}

        # Notify each patient AFTER the marker commits (its own session, never
        # nested). A notify failure must NEVER prevent the remaining reminders —
        # and, since the marker is already persisted, a failed one is simply not
        # retried on the next run.
        from sehaty.core.controllers.notifications import NotificationController

        for appointment_id, patient_id, doctor_id, start_at in pending:
            doctor_name = doctor_names.get(doctor_id, "your doctor")
            message = (
                f"Reminder: your appointment with {doctor_name} on {start_at:%Y-%m-%d} is coming up"
            )
            try:
                NotificationController.notify(
                    patient_id,
                    kind="appointment_reminder",
                    message=message,
                    entity="appointment",
                    entity_id=appointment_id,
                )
            except Exception:
                pass
        return len(pending)
