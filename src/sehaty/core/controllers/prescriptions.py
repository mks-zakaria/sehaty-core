"""Prescription business logic (freehand + catalog-linked).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A doctor
issues a :class:`Prescription` for any patient in their register — including
walk-ins with no app account. Each line is a :class:`PrescriptionItem` that is
EITHER catalog-linked (``medication_id``) or freehand (free-text ``drug_name``);
every prescription carries a short public ``code`` and an opaque ``qr_token`` for
verification, and is issued under a letterhead (``practice_profile_id``), so the
document stays printable even if the profile is later removed.

Everything is doctor-scoped: methods take ``doctor_id`` and filter by it, so a
register patient or prescription belonging to another doctor is a
:class:`SehatyNotFoundError`. Reads return small frozen dataclass projections
(never ORM objects). When the register patient is an app user (``user_id`` set),
the prescription's ``patient_id`` is linked to that account so the patient sees
it in their medical history. Failures raise the ``SehatyError`` taxonomy; all IO
flows through ``get_session``.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sehaty.db import (
    ClinicPatient,
    Medication,
    PracticeProfile,
    Prescription,
    PrescriptionItem,
    PrescriptionStatus,
)
from sqlalchemy import func, select

from sehaty.core.controllers.practice import (
    PracticeProfileController,
    PracticeProfileRow,
)
from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)

# Public verification code: short, unambiguous uppercase alphabet (no 0/O/1/I).
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LEN = 10
# Opaque QR token: hex bytes -> 32 hex chars.
_QR_BYTES = 16
_DEFAULT_EXPIRES_DAYS = 90


@dataclass(frozen=True)
class PrescriptionItemRow:
    """One line of a prescription (detached; ``drug_name`` already resolved)."""

    drug_name: str | None
    dosage: str
    frequency: str
    duration_days: int | None
    quantity: int
    instructions: str | None


@dataclass(frozen=True)
class PrescriptionSummary:
    """A prescription row for a list view (detached, column-only projection)."""

    id: int
    code: str
    status: str
    issued_at: datetime
    expires_at: datetime
    item_count: int


@dataclass(frozen=True)
class PrescriptionDetail:
    """A prescription with its items and (on ``get``) the letterhead snapshot."""

    id: int
    code: str
    status: str
    issued_at: datetime
    expires_at: datetime
    notes: str | None
    practice_profile_id: int | None
    items: list[PrescriptionItemRow]
    letterhead: PracticeProfileRow | None = None


class PrescriptionController:
    @staticmethod
    def create(
        doctor_id: int,
        clinic_patient_id: int,
        items: list[dict],
        practice_profile_id: int | None = None,
        appointment_id: int | None = None,
        notes: str | None = None,
        expires_days: int = _DEFAULT_EXPIRES_DAYS,
    ) -> PrescriptionDetail:
        """Issue a prescription for one of the doctor's register patients.

        The register patient must belong to ``doctor_id`` (else
        :class:`SehatyNotFoundError`). A given ``practice_profile_id`` must also
        belong to the doctor (else :class:`SehatyNotFoundError`); when omitted the
        doctor's default letterhead is used (may be ``None``).

        Each item dict needs EITHER ``medication_id`` OR a non-empty ``drug_name``
        plus required ``dosage`` and ``frequency`` (else
        :class:`SehatyValidationError`). ``quantity`` defaults to 0 when omitted
        (the column is NOT NULL). A unique ``code`` and ``qr_token`` are minted;
        status is ISSUED, issued_at is now (UTC) and expires_at is now +
        ``expires_days``. When the register row links an app user, that user's id
        is set as ``patient_id`` so app patients keep the account link. Returns
        the :class:`PrescriptionDetail`.
        """
        if not items:
            raise SehatyValidationError("a prescription needs at least one item")

        now = datetime.now(UTC)
        with get_session() as session:
            patient = session.execute(
                select(ClinicPatient.id, ClinicPatient.user_id).where(
                    ClinicPatient.id == clinic_patient_id,
                    ClinicPatient.doctor_id == doctor_id,
                )
            ).one_or_none()
            if patient is None:
                raise SehatyNotFoundError(
                    f"no patient {clinic_patient_id} in doctor {doctor_id}'s register"
                )

            if practice_profile_id is not None:
                owns = session.execute(
                    select(PracticeProfile.id).where(
                        PracticeProfile.id == practice_profile_id,
                        PracticeProfile.doctor_id == doctor_id,
                    )
                ).scalar_one_or_none()
                if owns is None:
                    raise SehatyNotFoundError(
                        f"no practice profile {practice_profile_id} for doctor {doctor_id}"
                    )
                profile_id = practice_profile_id
            else:
                default = PracticeProfileController.get_default(doctor_id)
                profile_id = default.id if default is not None else None

            built_items = [PrescriptionController._build_item(raw) for raw in items]

            code = PrescriptionController._generate_code(session)
            qr_token = PrescriptionController._generate_qr(session)

            prescription = Prescription(
                doctor_id=doctor_id,
                patient_id=patient.user_id,  # link the app account when present
                clinic_patient_id=clinic_patient_id,
                appointment_id=appointment_id,
                practice_profile_id=profile_id,
                code=code,
                qr_token=qr_token,
                status=PrescriptionStatus.ISSUED,
                notes=notes,
                issued_at=now,
                expires_at=now + timedelta(days=expires_days),
                items=built_items,
            )
            session.add(prescription)
            session.flush()

            item_rows = PrescriptionController._resolve_items(session, prescription.id)
            return PrescriptionDetail(
                id=prescription.id,
                code=prescription.code,
                status=str(prescription.status),
                issued_at=prescription.issued_at,
                expires_at=prescription.expires_at,
                notes=prescription.notes,
                practice_profile_id=prescription.practice_profile_id,
                items=item_rows,
            )

    @staticmethod
    def list_for_patient(doctor_id: int, clinic_patient_id: int) -> list[PrescriptionSummary]:
        """List a register patient's prescriptions, newest first.

        The register patient must belong to ``doctor_id`` (else
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
            return PrescriptionController._summaries(
                session,
                Prescription.doctor_id == doctor_id,
                Prescription.clinic_patient_id == clinic_patient_id,
            )

    @staticmethod
    def get(doctor_id: int, prescription_id: int) -> PrescriptionDetail:
        """Return one prescription with its items and letterhead snapshot.

        The prescription must belong to ``doctor_id`` (else
        :class:`SehatyNotFoundError`). ``letterhead`` is the practice-profile
        snapshot needed to render the printable document, or ``None`` if the
        profile was removed / never set.
        """
        with get_session() as session:
            prescription = session.execute(
                select(Prescription).where(
                    Prescription.id == prescription_id,
                    Prescription.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if prescription is None:
                raise SehatyNotFoundError(
                    f"no prescription {prescription_id} for doctor {doctor_id}"
                )
            return PrescriptionController._detail_with_letterhead(session, prescription)

    @staticmethod
    def get_for_app_patient(user_id: int, prescription_id: int) -> PrescriptionDetail:
        """Return one of the patient's OWN prescriptions with items + letterhead.

        Same :class:`PrescriptionDetail` shape as :meth:`get`, but authorised by
        the PATIENT: the prescription must exist (else
        :class:`SehatyNotFoundError`) and its register patient
        (``clinic_patient.user_id``) must be ``user_id`` (else
        :class:`SehatyForbiddenError`) — so a patient only ever sees the meds on
        their own prescriptions.
        """
        with get_session() as session:
            prescription = session.get(Prescription, prescription_id)
            if prescription is None:
                raise SehatyNotFoundError(f"no prescription {prescription_id}")

            owner_user_id = None
            if prescription.clinic_patient_id is not None:
                owner_user_id = session.execute(
                    select(ClinicPatient.user_id).where(
                        ClinicPatient.id == prescription.clinic_patient_id
                    )
                ).scalar_one_or_none()
            if owner_user_id != user_id:
                raise SehatyForbiddenError(
                    f"prescription {prescription_id} does not belong to user {user_id}"
                )

            return PrescriptionController._detail_with_letterhead(session, prescription)

    @staticmethod
    def list_for_app_patient(user_id: int) -> list[PrescriptionSummary]:
        """List an app patient's OWN prescriptions across doctors, newest first.

        Joins the register on ``user_id`` so a patient sees every prescription
        issued to any of their register rows (their medical-history view).
        """
        with get_session() as session:
            return PrescriptionController._summaries(
                session,
                ClinicPatient.user_id == user_id,
                _join=ClinicPatient,
            )

    @staticmethod
    def cancel(doctor_id: int, prescription_id: int) -> PrescriptionDetail:
        """Mark the doctor's prescription CANCELLED.

        The prescription must belong to ``doctor_id`` (else
        :class:`SehatyNotFoundError`). Returns the refreshed detail.
        """
        with get_session() as session:
            prescription = session.execute(
                select(Prescription).where(
                    Prescription.id == prescription_id,
                    Prescription.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if prescription is None:
                raise SehatyNotFoundError(
                    f"no prescription {prescription_id} for doctor {doctor_id}"
                )
            prescription.status = PrescriptionStatus.CANCELLED
            session.flush()
        return PrescriptionController.get(doctor_id, prescription_id)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _detail_with_letterhead(session, prescription: Prescription) -> PrescriptionDetail:
        """Build a full :class:`PrescriptionDetail` (items + letterhead snapshot).

        Shared by :meth:`get` and :meth:`get_for_app_patient` so both return the
        identical shape. ``letterhead`` is the practice-profile snapshot, or
        ``None`` if the profile was removed / never set.
        """
        letterhead: PracticeProfileRow | None = None
        if prescription.practice_profile_id is not None:
            profile = session.get(PracticeProfile, prescription.practice_profile_id)
            if profile is not None:
                from sehaty.core.controllers.practice import _row

                letterhead = _row(profile)

        item_rows = PrescriptionController._resolve_items(session, prescription.id)
        return PrescriptionDetail(
            id=prescription.id,
            code=prescription.code,
            status=str(prescription.status),
            issued_at=prescription.issued_at,
            expires_at=prescription.expires_at,
            notes=prescription.notes,
            practice_profile_id=prescription.practice_profile_id,
            items=item_rows,
            letterhead=letterhead,
        )

    @staticmethod
    def _build_item(raw: dict) -> PrescriptionItem:
        """Validate one item dict and build a (transient) PrescriptionItem.

        Requires EITHER ``medication_id`` OR a non-empty ``drug_name``, plus
        ``dosage`` and ``frequency``. ``quantity`` defaults to 0 (NOT NULL
        column). Raises :class:`SehatyValidationError` on any violation.
        """
        medication_id = raw.get("medication_id")
        drug_name = raw.get("drug_name")
        clean_name = (drug_name or "").strip() if drug_name is not None else ""
        if medication_id is None and not clean_name:
            raise SehatyValidationError(
                "each item needs either a medication_id or a non-empty drug_name"
            )

        dosage = (raw.get("dosage") or "").strip() if raw.get("dosage") is not None else ""
        frequency = (raw.get("frequency") or "").strip() if raw.get("frequency") is not None else ""
        if not dosage:
            raise SehatyValidationError("each item needs a dosage")
        if not frequency:
            raise SehatyValidationError("each item needs a frequency")

        quantity = raw.get("quantity")
        return PrescriptionItem(
            medication_id=medication_id,
            drug_name=clean_name or None,
            dosage=dosage,
            frequency=frequency,
            instructions=raw.get("instructions"),
            duration_days=raw.get("duration_days"),
            quantity=int(quantity) if quantity is not None else 0,
        )

    @staticmethod
    def _resolve_items(session, prescription_id: int) -> list[PrescriptionItemRow]:
        """Load a prescription's items, resolving catalog names for the UI."""
        items = (
            session.execute(
                select(PrescriptionItem)
                .where(PrescriptionItem.prescription_id == prescription_id)
                .order_by(PrescriptionItem.id.asc())
            )
            .scalars()
            .all()
        )
        med_ids = {i.medication_id for i in items if i.medication_id is not None}
        catalog: dict[int, str] = {}
        if med_ids:
            rows = session.execute(
                select(Medication.id, Medication.inn_name, Medication.brand_name).where(
                    Medication.id.in_(med_ids)
                )
            ).all()
            catalog = {r.id: (r.inn_name or r.brand_name) for r in rows}

        resolved: list[PrescriptionItemRow] = []
        for item in items:
            if item.medication_id is not None:
                name = catalog.get(item.medication_id) or item.drug_name
            else:
                name = item.drug_name
            resolved.append(
                PrescriptionItemRow(
                    drug_name=name,
                    dosage=item.dosage,
                    frequency=item.frequency,
                    duration_days=item.duration_days,
                    quantity=item.quantity,
                    instructions=item.instructions,
                )
            )
        return resolved

    @staticmethod
    def _summaries(session, *conditions, _join=None) -> list[PrescriptionSummary]:
        """Return prescription summaries with a live item count, newest first."""
        item_count = (
            select(
                PrescriptionItem.prescription_id.label("pid"),
                func.count(PrescriptionItem.id).label("n"),
            )
            .group_by(PrescriptionItem.prescription_id)
            .subquery()
        )
        stmt = select(
            Prescription.id,
            Prescription.code,
            Prescription.status,
            Prescription.issued_at,
            Prescription.expires_at,
            func.coalesce(item_count.c.n, 0).label("item_count"),
        ).outerjoin(item_count, item_count.c.pid == Prescription.id)
        if _join is not None:
            stmt = stmt.join(_join, ClinicPatient.id == Prescription.clinic_patient_id)
        stmt = stmt.where(*conditions).order_by(
            Prescription.issued_at.desc(), Prescription.id.desc()
        )
        rows = session.execute(stmt).all()
        return [
            PrescriptionSummary(
                id=r.id,
                code=r.code,
                status=str(r.status),
                issued_at=r.issued_at,
                expires_at=r.expires_at,
                item_count=int(r.item_count),
            )
            for r in rows
        ]

    @staticmethod
    def _generate_code(session) -> str:
        """Mint a short code unique across ``prescriptions.code``."""
        while True:
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
            exists = session.execute(
                select(Prescription.id).where(Prescription.code == code).limit(1)
            ).first()
            if exists is None:
                return code

    @staticmethod
    def _generate_qr(session) -> str:
        """Mint an opaque hex token unique across ``prescriptions.qr_token``."""
        while True:
            token = secrets.token_hex(_QR_BYTES)
            exists = session.execute(
                select(Prescription.id).where(Prescription.qr_token == token).limit(1)
            ).first()
            if exists is None:
                return token
