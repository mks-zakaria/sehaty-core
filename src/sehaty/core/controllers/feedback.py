"""Treatment-feedback business logic (patients rating how a treatment went).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A
:class:`~sehaty.db.TreatmentFeedback` row is a patient reporting whether a
prescription, diagnosis, or appointment left them BETTER / SAME / WORSE.

Authorization for ``leave`` is target-driven: we resolve the target's owning
``clinic_patient_id`` (prescription / diagnosis / appointment each carry one),
then require that register row's ``user_id`` to equal the author — a patient may
only rate their OWN records. An unknown target is a :class:`SehatyNotFoundError`;
someone else's record is a :class:`SehatyForbiddenError`. Feedback is
one-per-``(author, target)``: leaving again updates the existing row rather than
duplicating it.

The doctor-facing reads are doctor-scoped: they verify the register patient
belongs to ``doctor_id`` before returning that patient's feedback. Reads return
small frozen dataclass projections (never ORM objects). Failures raise the
``SehatyError`` taxonomy; methods never return ``None`` to signal an error.
"""

from dataclasses import dataclass
from datetime import datetime

from sehaty.db import (
    Appointment,
    ClinicPatient,
    Diagnosis,
    Prescription,
    TreatmentFeedback,
    TreatmentOutcome,
)
from sqlalchemy import select

from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)

# target_type -> the model carrying the clinic_patient_id that owns the target.
_TARGET_MODELS = {
    "prescription": Prescription,
    "diagnosis": Diagnosis,
    "appointment": Appointment,
}


@dataclass(frozen=True)
class FeedbackRow:
    """One treatment-feedback entry as a detached projection."""

    id: int
    target_type: str
    target_id: int
    outcome: str
    comment: str | None
    author_user_id: int | None
    created_at: datetime


def _row(f: TreatmentFeedback) -> FeedbackRow:
    return FeedbackRow(
        id=f.id,
        target_type=f.target_type,
        target_id=f.target_id,
        outcome=str(f.outcome),
        comment=f.comment,
        author_user_id=f.author_user_id,
        created_at=f.created_at,
    )


def _coerce_outcome(outcome: object) -> TreatmentOutcome:
    """Validate/coerce a caller-supplied outcome into a TreatmentOutcome."""
    if isinstance(outcome, TreatmentOutcome):
        return outcome
    try:
        return TreatmentOutcome(outcome)
    except ValueError as exc:
        valid = [o.value for o in TreatmentOutcome]
        raise SehatyValidationError(
            f"invalid outcome {outcome!r}; expected one of {valid}"
        ) from exc


class TreatmentFeedbackController:
    @staticmethod
    def leave(
        author_user_id: int,
        target_type: str,
        target_id: int,
        outcome: object,
        comment: str | None = None,
    ) -> FeedbackRow:
        """Leave (or update) the author's outcome on one of their OWN records.

        Resolves the target's owning register row via ``target_type`` /
        ``target_id`` (an unknown ``target_type`` or missing target is a
        :class:`SehatyNotFoundError`) and requires that row's ``user_id`` to be
        ``author_user_id`` (else :class:`SehatyForbiddenError`). ``outcome`` is
        validated/coerced to a :class:`TreatmentOutcome` (invalid ->
        :class:`SehatyValidationError`). One feedback per ``(author, target)``:
        an existing row is updated in place, otherwise a new one is created.
        """
        model = _TARGET_MODELS.get(target_type)
        if model is None:
            raise SehatyNotFoundError(f"unknown target_type {target_type!r}")

        resolved = _coerce_outcome(outcome)

        with get_session() as session:
            clinic_patient_id = session.execute(
                select(model.clinic_patient_id).where(model.id == target_id)
            ).scalar_one_or_none()
            if clinic_patient_id is None:
                raise SehatyNotFoundError(
                    f"no {target_type} {target_id} (or it has no register patient)"
                )

            owner_user_id = session.execute(
                select(ClinicPatient.user_id).where(ClinicPatient.id == clinic_patient_id)
            ).scalar_one_or_none()
            if owner_user_id != author_user_id:
                raise SehatyForbiddenError(
                    f"user {author_user_id} may not leave feedback on {target_type} {target_id}"
                )

            existing = session.execute(
                select(TreatmentFeedback).where(
                    TreatmentFeedback.author_user_id == author_user_id,
                    TreatmentFeedback.target_type == target_type,
                    TreatmentFeedback.target_id == target_id,
                )
            ).scalar_one_or_none()

            if existing is not None:
                existing.outcome = resolved
                existing.comment = comment
                session.flush()
                return _row(existing)

            feedback = TreatmentFeedback(
                clinic_patient_id=clinic_patient_id,
                author_user_id=author_user_id,
                target_type=target_type,
                target_id=target_id,
                outcome=resolved,
                comment=comment,
            )
            session.add(feedback)
            session.flush()
            return _row(feedback)

    @staticmethod
    def list_for_patient(doctor_id: int, clinic_patient_id: int) -> list[FeedbackRow]:
        """List all feedback for one register patient, newest first (doctor view).

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
                    select(TreatmentFeedback)
                    .where(TreatmentFeedback.clinic_patient_id == clinic_patient_id)
                    .order_by(TreatmentFeedback.created_at.desc(), TreatmentFeedback.id.desc())
                )
                .scalars()
                .all()
            )
            return [_row(f) for f in rows]

    @staticmethod
    def for_targets(doctor_id: int, clinic_patient_id: int) -> dict[tuple[str, int], FeedbackRow]:
        """Map each ``(target_type, target_id)`` to its latest feedback row.

        Convenience for the doctor UI to attach the most recent feedback to each
        history item. The register row must belong to ``doctor_id`` (else
        :class:`SehatyNotFoundError`). Iterating newest-first and keeping the
        first per key yields the latest row per target.
        """
        latest: dict[tuple[str, int], FeedbackRow] = {}
        for row in TreatmentFeedbackController.list_for_patient(doctor_id, clinic_patient_id):
            key = (row.target_type, row.target_id)
            if key not in latest:
                latest[key] = row
        return latest
