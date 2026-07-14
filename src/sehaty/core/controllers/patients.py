"""Patient-register business logic (clinic_patients).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A
doctor/assistant keeps a register of ALL their patients — app users who booked
(``user_id`` links the account) and walk-in/phone patients with no account
(``user_id`` is NULL) — each row carrying denormalised contact + demographic
fields plus live-aggregated visit stats. This is the data behind a
spreadsheet-like patient grid.

Everything here is doctor-scoped: every method takes ``doctor_id`` and filters
by it, so a register row belonging to another doctor is a
:class:`SehatyNotFoundError`. Reads return small frozen dataclass projections
(never ORM objects), so callers hold detached, serialisable data.

The list aggregation is ONE column-only ``select(...).group_by(...)`` over the
appointments joined on ``clinic_patient_id``, using ``func.count`` / ``func.max``
/ ``func.min`` plus ``func.sum(case((cond, 1), else_=0))`` for the
status-conditional counts — portable on both SQLite (tests) and Postgres
(avoids the non-portable ``.filter()`` aggregate syntax). Failures raise the
``SehatyError`` taxonomy; methods never return ``None`` to signal an error.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sehaty.db import Appointment, AppointmentStatus, ClinicPatient
from sqlalchemy import case, func, select

from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError

# Statuses that count as an actual (past) visit for last_visit_at.
_VISITED = (AppointmentStatus.COMPLETED,)
# Statuses that count as an upcoming appointment for next_appointment_at.
_UPCOMING = (AppointmentStatus.REQUESTED, AppointmentStatus.CONFIRMED)

_SORTS = frozenset({"name", "recent", "visits", "upcoming"})

# Fields a doctor may set on their own register row via update_patient.
_UPDATABLE = ("full_name", "phone", "email", "sex", "birth_year", "notes", "tags")


@dataclass(frozen=True)
class PatientRow:
    """One row of the patient-register grid (detached, aggregated projection)."""

    id: int
    user_id: int | None
    full_name: str | None
    phone: str | None
    email: str | None
    tags: list[str] | None
    total_visits: int
    completed_visits: int
    no_show_count: int
    last_visit_at: datetime | None
    next_appointment_at: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class VisitRow:
    """One appointment in a patient's visit history (detached projection)."""

    appointment_id: int
    start_at: datetime
    end_at: datetime
    status: str
    reason: str | None
    notes: str | None


@dataclass(frozen=True)
class PatientDetail:
    """A single register row with full demographics + visit history."""

    id: int
    user_id: int | None
    full_name: str | None
    phone: str | None
    email: str | None
    sex: str | None
    birth_year: int | None
    notes: str | None
    tags: list[str] | None
    no_show_count: int
    total_visits: int
    created_at: datetime
    history: list[VisitRow]


class PatientRegisterController:
    @staticmethod
    def list_patients(
        doctor_id: int,
        search: str | None = None,
        sort: str = "name",
        limit: int = 50,
        offset: int = 0,
    ) -> list[PatientRow]:
        """List the doctor's register rows with live-aggregated visit stats.

        One column-only grouped query joins each :class:`ClinicPatient` (left) to
        its appointments on ``clinic_patient_id``: ``total_visits`` counts all
        appointments, ``completed_visits`` / live ``no_show_count`` use
        ``func.sum(case(...))``, ``last_visit_at`` is the max ``start_at`` among
        COMPLETED appointments, and ``next_appointment_at`` is the min
        ``start_at`` among REQUESTED/CONFIRMED appointments at or after ``now``.

        ``search`` matches ``full_name`` / ``phone`` / ``email`` (case-insensitive
        contains). ``sort`` is one of ``name`` (default; full_name, NULLs last),
        ``recent`` (last_visit_at desc), ``visits`` (total_visits desc), or
        ``upcoming`` (next_appointment_at asc, NULLs last); an unknown value
        raises :class:`SehatyValidationError`.
        """
        if sort not in _SORTS:
            raise SehatyValidationError(f"invalid sort {sort!r}; expected one of {sorted(_SORTS)}")
        now = datetime.now(UTC)

        completed = func.sum(case((Appointment.status == AppointmentStatus.COMPLETED, 1), else_=0))
        no_shows = func.sum(case((Appointment.status == AppointmentStatus.NO_SHOW, 1), else_=0))
        last_visit = func.max(case((Appointment.status.in_(_VISITED), Appointment.start_at)))
        next_appt = func.min(
            case(
                (
                    Appointment.status.in_(_UPCOMING) & (Appointment.start_at >= now),
                    Appointment.start_at,
                )
            )
        )
        total = func.count(Appointment.id)

        stmt = (
            select(
                ClinicPatient.id,
                ClinicPatient.user_id,
                ClinicPatient.full_name,
                ClinicPatient.phone,
                ClinicPatient.email,
                ClinicPatient.tags,
                ClinicPatient.created_at,
                total.label("total_visits"),
                completed.label("completed_visits"),
                no_shows.label("no_show_count"),
                last_visit.label("last_visit_at"),
                next_appt.label("next_appointment_at"),
            )
            .outerjoin(Appointment, Appointment.clinic_patient_id == ClinicPatient.id)
            .where(ClinicPatient.doctor_id == doctor_id)
            .group_by(ClinicPatient.id)
        )

        if search:
            like = f"%{search.lower()}%"
            stmt = stmt.where(
                func.lower(func.coalesce(ClinicPatient.full_name, "")).like(like)
                | func.lower(func.coalesce(ClinicPatient.phone, "")).like(like)
                | func.lower(func.coalesce(ClinicPatient.email, "")).like(like)
            )

        if sort == "name":
            stmt = stmt.order_by(
                (ClinicPatient.full_name.is_(None)),
                ClinicPatient.full_name.asc(),
                ClinicPatient.id.asc(),
            )
        elif sort == "recent":
            stmt = stmt.order_by(last_visit.desc().nulls_last(), ClinicPatient.id.asc())
        elif sort == "visits":
            stmt = stmt.order_by(total.desc(), ClinicPatient.id.asc())
        else:  # "upcoming"
            stmt = stmt.order_by(next_appt.asc().nulls_last(), ClinicPatient.id.asc())

        stmt = stmt.limit(limit).offset(offset)

        with get_session() as session:
            rows = session.execute(stmt).all()
        return [
            PatientRow(
                id=row.id,
                user_id=row.user_id,
                full_name=row.full_name,
                phone=row.phone,
                email=row.email,
                tags=row.tags,
                total_visits=int(row.total_visits),
                completed_visits=int(row.completed_visits or 0),
                no_show_count=int(row.no_show_count or 0),
                last_visit_at=row.last_visit_at,
                next_appointment_at=row.next_appointment_at,
                created_at=row.created_at,
            )
            for row in rows
        ]

    @staticmethod
    def get_patient(doctor_id: int, patient_id: int) -> PatientDetail:
        """Return one register row with demographics + full visit history.

        The row must belong to ``doctor_id`` (else :class:`SehatyNotFoundError`).
        ``history`` is every appointment on this register row, newest first.
        """
        with get_session() as session:
            row = session.execute(
                select(
                    ClinicPatient.id,
                    ClinicPatient.user_id,
                    ClinicPatient.full_name,
                    ClinicPatient.phone,
                    ClinicPatient.email,
                    ClinicPatient.sex,
                    ClinicPatient.birth_year,
                    ClinicPatient.notes,
                    ClinicPatient.tags,
                    ClinicPatient.no_show_count,
                    ClinicPatient.created_at,
                ).where(
                    ClinicPatient.id == patient_id,
                    ClinicPatient.doctor_id == doctor_id,
                )
            ).one_or_none()
            if row is None:
                raise SehatyNotFoundError(
                    f"no patient {patient_id} in doctor {doctor_id}'s register"
                )

            visits = session.execute(
                select(
                    Appointment.id,
                    Appointment.start_at,
                    Appointment.end_at,
                    Appointment.status,
                    Appointment.reason,
                    Appointment.notes,
                )
                .where(Appointment.clinic_patient_id == patient_id)
                .order_by(Appointment.start_at.desc(), Appointment.id.desc())
            ).all()
            total_visits = session.execute(
                select(func.count(Appointment.id)).where(
                    Appointment.clinic_patient_id == patient_id
                )
            ).scalar_one()

        history = [
            VisitRow(
                appointment_id=v.id,
                start_at=v.start_at,
                end_at=v.end_at,
                status=str(v.status),
                reason=v.reason,
                notes=v.notes,
            )
            for v in visits
        ]
        return PatientDetail(
            id=row.id,
            user_id=row.user_id,
            full_name=row.full_name,
            phone=row.phone,
            email=row.email,
            sex=row.sex,
            birth_year=row.birth_year,
            notes=row.notes,
            tags=row.tags,
            no_show_count=int(row.no_show_count),
            total_visits=int(total_visits),
            created_at=row.created_at,
            history=history,
        )

    @staticmethod
    def add_patient(
        doctor_id: int,
        created_by: int,
        full_name: str,
        phone: str | None = None,
        email: str | None = None,
        sex: str | None = None,
        birth_year: int | None = None,
        notes: str | None = None,
        tags: list[str] | None = None,
    ) -> PatientRow:
        """Create a walk-in / manually-entered register row (``user_id`` NULL).

        ``full_name`` must be non-empty (else :class:`SehatyValidationError`).
        Returns the new row as a :class:`PatientRow` (zeroed aggregates — a
        manual patient has no appointments yet).
        """
        name = (full_name or "").strip()
        if not name:
            raise SehatyValidationError("full_name must not be empty")
        with get_session() as session:
            patient = ClinicPatient(
                doctor_id=doctor_id,
                user_id=None,
                full_name=name,
                phone=phone,
                email=email,
                sex=sex,
                birth_year=birth_year,
                notes=notes,
                tags=tags if tags is not None else [],
                created_by=created_by,
            )
            session.add(patient)
            session.flush()
            new = PatientRow(
                id=patient.id,
                user_id=None,
                full_name=patient.full_name,
                phone=patient.phone,
                email=patient.email,
                tags=patient.tags,
                total_visits=0,
                completed_visits=0,
                no_show_count=int(patient.no_show_count),
                last_visit_at=None,
                next_appointment_at=None,
                created_at=patient.created_at,
            )
        return new

    @staticmethod
    def update_patient(doctor_id: int, patient_id: int, **fields: object) -> PatientDetail:
        """Update the allowed fields on the doctor's own register row.

        Only ``full_name`` / ``phone`` / ``email`` / ``sex`` / ``birth_year`` /
        ``notes`` / ``tags`` are writable; unknown keys are ignored. The row must
        belong to ``doctor_id`` (else :class:`SehatyNotFoundError`). Returns the
        refreshed :class:`PatientDetail`.
        """
        updates = {k: v for k, v in fields.items() if k in _UPDATABLE}
        with get_session() as session:
            patient = session.execute(
                select(ClinicPatient).where(
                    ClinicPatient.id == patient_id,
                    ClinicPatient.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if patient is None:
                raise SehatyNotFoundError(
                    f"no patient {patient_id} in doctor {doctor_id}'s register"
                )
            for key, value in updates.items():
                setattr(patient, key, value)
            session.flush()
        return PatientRegisterController.get_patient(doctor_id, patient_id)
