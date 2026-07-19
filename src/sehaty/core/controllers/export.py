"""Doctor data export — gather a doctor's records into tabular sheets.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). Doctors
still live in Excel, so the portal lets them pull their whole practice as a
workbook at any time. This controller owns the *data*: doctor-scoped, column-only
selects (no PostGIS ``geopoint``, no dialect-specific functions, so the same code
runs on SQLite and Postgres) flattened into ``ExportSheet`` row sets. Turning
those sheets into an actual ``.xlsx`` file is a transport concern that lives in
``sehaty-api``.
"""

import json
from dataclasses import dataclass
from datetime import datetime

from sehaty.db import (
    Appointment,
    ClinicPatient,
    Diagnosis,
    Invoice,
    Prescription,
    Review,
    ReviewDirection,
)
from sqlalchemy import select

from sehaty.core.db.session import get_session


@dataclass(frozen=True)
class ExportSheet:
    """One worksheet: a title, a header row, and the data rows (cells are scalars)."""

    title: str
    columns: list[str]
    rows: list[list[object]]


def _cell(value: object) -> object:
    """Coerce a value into an Excel-friendly scalar (str / int / float / None)."""
    if value is None or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    # StrEnum and anything else → its string form.
    return str(value)


class ExportController:
    @staticmethod
    def doctor_export(doctor_id: int) -> list[ExportSheet]:
        """Every sheet of a doctor's practice data, ready to render as a workbook.

        Sheets: Patients (the register), Appointments, Consultations (appointments
        the doctor actually started/finished, with the recorded encounter),
        Diagnoses, Prescriptions, Reviews (patient reviews about the doctor), and
        Billing (the doctor's invoices). All are scoped to ``doctor_id`` and
        ordered newest-first.
        """
        with get_session() as session:
            patients = ExportController._patients(session, doctor_id)
            names = ExportController._name_map(session, doctor_id)
            appointments = ExportController._appointments(session, doctor_id, names)
            consultations = ExportController._consultations(session, doctor_id, names)
            diagnoses = ExportController._diagnoses(session, doctor_id, names)
            prescriptions = ExportController._prescriptions(session, doctor_id, names)
            reviews = ExportController._reviews(session, doctor_id)
            billing = ExportController._billing(session, doctor_id)
        return [
            patients, appointments, consultations, diagnoses, prescriptions, reviews, billing,
        ]

    @staticmethod
    def _name_map(session, doctor_id: int) -> dict[int, str]:
        """clinic_patient_id → human name, for labelling the other sheets."""
        rows = session.execute(
            select(ClinicPatient.id, ClinicPatient.full_name).where(
                ClinicPatient.doctor_id == doctor_id
            )
        ).all()
        return {cid: (name or f"Patient #{cid}") for cid, name in rows}

    @staticmethod
    def _patients(session, doctor_id: int) -> ExportSheet:
        cols = [
            "id", "name", "phone", "email", "sex", "birth_year",
            "no_show_count", "tags", "notes", "created_at",
        ]
        rows = session.execute(
            select(
                ClinicPatient.id,
                ClinicPatient.full_name,
                ClinicPatient.phone,
                ClinicPatient.email,
                ClinicPatient.sex,
                ClinicPatient.birth_year,
                ClinicPatient.no_show_count,
                ClinicPatient.tags,
                ClinicPatient.notes,
                ClinicPatient.created_at,
            )
            .where(ClinicPatient.doctor_id == doctor_id)
            .order_by(ClinicPatient.id.desc())
        ).all()
        return ExportSheet("Patients", cols, [[_cell(v) for v in r] for r in rows])

    @staticmethod
    def _appointments(session, doctor_id: int, names: dict[int, str]) -> ExportSheet:
        cols = ["id", "patient", "start_at", "end_at", "status", "reason"]
        rows = session.execute(
            select(
                Appointment.id,
                Appointment.clinic_patient_id,
                Appointment.start_at,
                Appointment.end_at,
                Appointment.status,
                Appointment.reason,
            )
            .where(Appointment.doctor_id == doctor_id)
            .order_by(Appointment.start_at.desc())
        ).all()
        out = [
            [_cell(r.id), names.get(r.clinic_patient_id), _cell(r.start_at),
             _cell(r.end_at), _cell(r.status), _cell(r.reason)]
            for r in rows
        ]
        return ExportSheet("Appointments", cols, out)

    @staticmethod
    def _consultations(session, doctor_id: int, names: dict[int, str]) -> ExportSheet:
        cols = [
            "appointment_id", "patient", "started_at", "ended_at",
            "chief_complaint", "symptoms", "vitals", "exam_notes",
        ]
        rows = session.execute(
            select(
                Appointment.id,
                Appointment.clinic_patient_id,
                Appointment.consultation_started_at,
                Appointment.consultation_ended_at,
                Appointment.chief_complaint,
                Appointment.symptoms,
                Appointment.vitals,
                Appointment.exam_notes,
            )
            .where(
                Appointment.doctor_id == doctor_id,
                Appointment.consultation_started_at.is_not(None),
            )
            .order_by(Appointment.consultation_started_at.desc())
        ).all()
        out = [
            [_cell(r.id), names.get(r.clinic_patient_id), _cell(r.consultation_started_at),
             _cell(r.consultation_ended_at), _cell(r.chief_complaint), _cell(r.symptoms),
             _cell(r.vitals), _cell(r.exam_notes)]
            for r in rows
        ]
        return ExportSheet("Consultations", cols, out)

    @staticmethod
    def _diagnoses(session, doctor_id: int, names: dict[int, str]) -> ExportSheet:
        cols = ["id", "patient", "label", "icd10", "notes", "diagnosed_at"]
        rows = session.execute(
            select(
                Diagnosis.id,
                Diagnosis.clinic_patient_id,
                Diagnosis.label,
                Diagnosis.icd10,
                Diagnosis.notes,
                Diagnosis.diagnosed_at,
            )
            .where(Diagnosis.doctor_id == doctor_id)
            .order_by(Diagnosis.diagnosed_at.desc())
        ).all()
        out = [
            [_cell(r.id), names.get(r.clinic_patient_id), _cell(r.label),
             _cell(r.icd10), _cell(r.notes), _cell(r.diagnosed_at)]
            for r in rows
        ]
        return ExportSheet("Diagnoses", cols, out)

    @staticmethod
    def _prescriptions(session, doctor_id: int, names: dict[int, str]) -> ExportSheet:
        cols = ["id", "patient", "code", "status", "issued_at", "expires_at", "notes"]
        rows = session.execute(
            select(
                Prescription.id,
                Prescription.clinic_patient_id,
                Prescription.code,
                Prescription.status,
                Prescription.issued_at,
                Prescription.expires_at,
                Prescription.notes,
            )
            .where(Prescription.doctor_id == doctor_id)
            .order_by(Prescription.issued_at.desc())
        ).all()
        out = [
            [_cell(r.id), names.get(r.clinic_patient_id), _cell(r.code), _cell(r.status),
             _cell(r.issued_at), _cell(r.expires_at), _cell(r.notes)]
            for r in rows
        ]
        return ExportSheet("Prescriptions", cols, out)

    @staticmethod
    def _reviews(session, doctor_id: int) -> ExportSheet:
        cols = ["id", "stars", "comment", "status", "reply", "reply_at", "created_at"]
        rows = session.execute(
            select(
                Review.id,
                Review.stars,
                Review.comment,
                Review.status,
                Review.reply,
                Review.reply_at,
                Review.created_at,
            )
            .where(
                Review.target_id == doctor_id,
                Review.direction == ReviewDirection.PATIENT_ON_DOCTOR,
            )
            .order_by(Review.created_at.desc())
        ).all()
        return ExportSheet("Reviews", cols, [[_cell(v) for v in r] for r in rows])

    @staticmethod
    def _billing(session, doctor_id: int) -> ExportSheet:
        cols = ["invoice_id", "amount", "currency", "status", "issued_at", "due_at", "paid_at"]
        rows = session.execute(
            select(
                Invoice.id,
                Invoice.amount,
                Invoice.currency,
                Invoice.status,
                Invoice.issued_at,
                Invoice.due_at,
                Invoice.paid_at,
            )
            .where(Invoice.doctor_id == doctor_id)
            .order_by(Invoice.issued_at.desc())
        ).all()
        return ExportSheet("Billing", cols, [[_cell(v) for v in r] for r in rows])
