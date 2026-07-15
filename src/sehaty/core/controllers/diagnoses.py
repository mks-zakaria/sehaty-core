"""Diagnosis business logic (a doctor's record of a patient's condition).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A
:class:`~sehaty.db.Diagnosis` is a disease/condition a doctor recorded for one
register patient (:class:`~sehaty.db.ClinicPatient`). Doctor-facing methods are
doctor-scoped: they take ``doctor_id`` and verify the target register row (or
the diagnosis' own ``doctor_id``) belongs to that doctor, so anything owned by
another doctor is a :class:`SehatyNotFoundError`.

``list_for_app_patient`` is the one patient-facing read: it joins
``clinic_patients`` on ``user_id`` so an app patient can see their OWN
diagnoses across every doctor whose register they appear in (their
medical-history view). Reads return small frozen dataclass projections (never
ORM objects), so callers hold detached, serialisable data. Failures raise the
``SehatyError`` taxonomy; methods never return ``None`` to signal an error.
"""

from dataclasses import dataclass
from datetime import datetime

from sehaty.db import ClinicPatient, Diagnosis
from sehaty.db.base import utcnow
from sqlalchemy import select

from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError

# Fields a doctor may change on their own diagnosis via update().
_UPDATABLE = ("label", "icd10", "notes", "appointment_id", "diagnosed_at")


@dataclass(frozen=True)
class DiagnosisRow:
    """One diagnosis as a detached projection."""

    id: int
    label: str
    icd10: str | None
    notes: str | None
    diagnosed_at: datetime
    appointment_id: int | None


def _row(d: Diagnosis) -> DiagnosisRow:
    return DiagnosisRow(
        id=d.id,
        label=d.label,
        icd10=d.icd10,
        notes=d.notes,
        diagnosed_at=d.diagnosed_at,
        appointment_id=d.appointment_id,
    )


class DiagnosisController:
    @staticmethod
    def create(
        doctor_id: int,
        clinic_patient_id: int,
        label: str,
        icd10: str | None = None,
        notes: str | None = None,
        appointment_id: int | None = None,
        diagnosed_at: datetime | None = None,
    ) -> DiagnosisRow:
        """Record a diagnosis for one of the doctor's own register patients.

        The register row must belong to ``doctor_id`` (else
        :class:`SehatyNotFoundError`). ``label`` must be non-empty (else
        :class:`SehatyValidationError`). ``diagnosed_at`` defaults to now (UTC).
        """
        clean_label = (label or "").strip()
        if not clean_label:
            raise SehatyValidationError("label must not be empty")

        with get_session() as session:
            owns = session.execute(
                select(ClinicPatient.id).where(
                    ClinicPatient.id == clinic_patient_id,
                    ClinicPatient.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if owns is None:
                raise SehatyNotFoundError(
                    f"no patient {clinic_patient_id} in doctor {doctor_id}'s register"
                )

            diagnosis = Diagnosis(
                doctor_id=doctor_id,
                clinic_patient_id=clinic_patient_id,
                appointment_id=appointment_id,
                label=clean_label,
                icd10=icd10,
                notes=notes,
                diagnosed_at=diagnosed_at or utcnow(),
            )
            session.add(diagnosis)
            session.flush()
            return _row(diagnosis)

    @staticmethod
    def list_for_patient(doctor_id: int, clinic_patient_id: int) -> list[DiagnosisRow]:
        """List a register patient's diagnoses, newest first (doctor view).

        The register row must belong to ``doctor_id`` (else
        :class:`SehatyNotFoundError`).
        """
        with get_session() as session:
            owns = session.execute(
                select(ClinicPatient.id).where(
                    ClinicPatient.id == clinic_patient_id,
                    ClinicPatient.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if owns is None:
                raise SehatyNotFoundError(
                    f"no patient {clinic_patient_id} in doctor {doctor_id}'s register"
                )

            rows = (
                session.execute(
                    select(Diagnosis)
                    .where(Diagnosis.clinic_patient_id == clinic_patient_id)
                    .order_by(Diagnosis.diagnosed_at.desc(), Diagnosis.id.desc())
                )
                .scalars()
                .all()
            )
            return [_row(d) for d in rows]

    @staticmethod
    def list_for_app_patient(user_id: int) -> list[DiagnosisRow]:
        """List an app patient's OWN diagnoses, newest first (patient view).

        Joins ``clinic_patients`` on ``user_id`` so the patient sees every
        diagnosis recorded against a register row that links to their account,
        across all their doctors.
        """
        with get_session() as session:
            rows = (
                session.execute(
                    select(Diagnosis)
                    .join(ClinicPatient, Diagnosis.clinic_patient_id == ClinicPatient.id)
                    .where(ClinicPatient.user_id == user_id)
                    .order_by(Diagnosis.diagnosed_at.desc(), Diagnosis.id.desc())
                )
                .scalars()
                .all()
            )
            return [_row(d) for d in rows]

    @staticmethod
    def update(doctor_id: int, diagnosis_id: int, **fields: object) -> DiagnosisRow:
        """Update the allowed fields on the doctor's own diagnosis.

        Only ``label`` / ``icd10`` / ``notes`` / ``appointment_id`` /
        ``diagnosed_at`` are writable; unknown keys are ignored. The diagnosis
        must belong to ``doctor_id`` (else :class:`SehatyNotFoundError`). A
        blank ``label`` raises :class:`SehatyValidationError`.
        """
        updates = {k: v for k, v in fields.items() if k in _UPDATABLE}
        if "label" in updates:
            label = str(updates["label"] or "").strip()
            if not label:
                raise SehatyValidationError("label must not be empty")
            updates["label"] = label

        with get_session() as session:
            diagnosis = session.execute(
                select(Diagnosis).where(
                    Diagnosis.id == diagnosis_id,
                    Diagnosis.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if diagnosis is None:
                raise SehatyNotFoundError(f"no diagnosis {diagnosis_id} for doctor {doctor_id}")
            for key, value in updates.items():
                setattr(diagnosis, key, value)
            session.flush()
            return _row(diagnosis)

    @staticmethod
    def delete(doctor_id: int, diagnosis_id: int) -> None:
        """Delete the doctor's own diagnosis.

        The diagnosis must belong to ``doctor_id`` (else
        :class:`SehatyNotFoundError`).
        """
        with get_session() as session:
            diagnosis = session.execute(
                select(Diagnosis).where(
                    Diagnosis.id == diagnosis_id,
                    Diagnosis.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if diagnosis is None:
                raise SehatyNotFoundError(f"no diagnosis {diagnosis_id} for doctor {doctor_id}")
            session.delete(diagnosis)
