"""Prescription-template business logic (a doctor's reusable prescription presets).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A
:class:`~sehaty.db.PrescriptionTemplate` is a doctor's named, reusable preset
(e.g. "Angine - adulte") that pre-builds a common prescription once so it can be
dropped into a new prescription in one click. ``items`` is a JSON list of
freehand medication rows: ``{drug_name, dosage, frequency, duration_days,
instructions}`` dicts.

Everything is doctor-scoped: methods take ``doctor_id`` and filter by it, so a
template belonging to another doctor is a :class:`SehatyNotFoundError`. Item
validation mirrors the freehand rules used by
:class:`~sehaty.core.controllers.prescriptions.PrescriptionController` (a
non-empty ``drug_name`` plus required ``dosage`` and ``frequency``;
``duration_days`` / ``instructions`` optional). Reads return small frozen
dataclass projections (never ORM objects). Failures raise the ``SehatyError``
taxonomy; all IO flows through ``get_session``.
"""

from datetime import datetime

from sehaty.db import PrescriptionTemplate
from sqlalchemy import select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError


class TemplateItem(DomainModel):
    """One freehand medication row of a template (detached projection)."""

    drug_name: str
    dosage: str
    frequency: str
    duration_days: int | None
    instructions: str | None


class TemplateRow(DomainModel):
    """A prescription template as a detached projection (items already resolved)."""

    id: int
    name: str
    notes: str | None
    items: list[TemplateItem]
    created_at: datetime


def _item_row(raw: dict) -> TemplateItem:
    """Project a stored item dict onto a :class:`TemplateItem`."""
    return TemplateItem(
        drug_name=raw.get("drug_name"),
        dosage=raw.get("dosage"),
        frequency=raw.get("frequency"),
        duration_days=raw.get("duration_days"),
        instructions=raw.get("instructions"),
    )


def _row(template: PrescriptionTemplate) -> TemplateRow:
    return TemplateRow(
        id=template.id,
        name=template.name,
        notes=template.notes,
        items=[_item_row(i) for i in template.items],
        created_at=template.created_at,
    )


class PrescriptionTemplateController:
    @staticmethod
    def create(
        doctor_id: int,
        name: str,
        items: list[dict],
        notes: str | None = None,
    ) -> TemplateRow:
        """Create a reusable prescription template for the doctor.

        ``name`` must be non-empty (else :class:`SehatyValidationError`).
        ``items`` must be a non-empty list where each row has a non-empty
        ``drug_name`` plus required ``dosage`` and ``frequency`` (mirrors the
        prescription freehand-item validation; ``duration_days`` /
        ``instructions`` optional) — else :class:`SehatyValidationError`. Each
        item is normalised to the stored dict shape. Returns the
        :class:`TemplateRow`.
        """
        clean_name = (name or "").strip()
        if not clean_name:
            raise SehatyValidationError("name must not be empty")
        if not items:
            raise SehatyValidationError("a template needs at least one item")

        built_items = [PrescriptionTemplateController._build_item(raw) for raw in items]

        with get_session() as session:
            template = PrescriptionTemplate(
                doctor_id=doctor_id,
                name=clean_name,
                notes=notes,
                items=built_items,
            )
            session.add(template)
            session.flush()
            return _row(template)

    @staticmethod
    def list_for(doctor_id: int) -> list[TemplateRow]:
        """List the doctor's templates, ordered by name (case-insensitive)."""
        with get_session() as session:
            rows = (
                session.execute(
                    select(PrescriptionTemplate)
                    .where(PrescriptionTemplate.doctor_id == doctor_id)
                    .order_by(PrescriptionTemplate.name.asc(), PrescriptionTemplate.id.asc())
                )
                .scalars()
                .all()
            )
            return [_row(t) for t in rows]

    @staticmethod
    def delete(doctor_id: int, template_id: int) -> None:
        """Delete the doctor's own template.

        The template must belong to ``doctor_id`` (else
        :class:`SehatyNotFoundError`).
        """
        with get_session() as session:
            template = session.execute(
                select(PrescriptionTemplate).where(
                    PrescriptionTemplate.id == template_id,
                    PrescriptionTemplate.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if template is None:
                raise SehatyNotFoundError(f"no template {template_id} for doctor {doctor_id}")
            session.delete(template)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_item(raw: dict) -> dict:
        """Validate one item dict and normalise it to the stored dict shape.

        Requires a non-empty ``drug_name`` plus ``dosage`` and ``frequency``
        (mirrors :meth:`PrescriptionController._build_item`'s freehand path).
        ``duration_days`` / ``instructions`` are optional. Raises
        :class:`SehatyValidationError` on any violation.
        """
        drug_name = raw.get("drug_name")
        clean_name = (drug_name or "").strip() if drug_name is not None else ""
        if not clean_name:
            raise SehatyValidationError("each item needs a non-empty drug_name")

        dosage = (raw.get("dosage") or "").strip() if raw.get("dosage") is not None else ""
        frequency = (raw.get("frequency") or "").strip() if raw.get("frequency") is not None else ""
        if not dosage:
            raise SehatyValidationError("each item needs a dosage")
        if not frequency:
            raise SehatyValidationError("each item needs a frequency")

        duration_days = raw.get("duration_days")
        return {
            "drug_name": clean_name,
            "dosage": dosage,
            "frequency": frequency,
            "duration_days": int(duration_days) if duration_days is not None else None,
            "instructions": raw.get("instructions"),
        }
