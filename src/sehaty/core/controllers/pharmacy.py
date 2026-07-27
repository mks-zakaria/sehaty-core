"""Pharmacy dispensing: look up a prescription and dispense its items.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A pharmacy
(a ``PHARMACY`` user) scans/enters a prescription's public ``code`` to see its
lines and how much is still owed, then records a dispense: it creates a
:class:`Dispense` (+ :class:`DispenseItem` lines), bumps each
``PrescriptionItem.quantity_dispensed`` (never past the prescribed quantity), and
decrements the pharmacy's :class:`PharmacyStock` for catalog-linked drugs when a
stock row exists. Every dispense writes an ``AuditLog`` entry. Reads return
detached ``DomainModel`` projections; failures raise the ``SehatyError`` taxonomy.
"""

from datetime import UTC, datetime

from sehaty.db import (
    AuditLog,
    Dispense,
    DispenseItem,
    Medication,
    PharmacyStock,
    Prescription,
    PrescriptionItem,
    PrescriptionStatus,
)
from sqlalchemy import select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyNotFoundError,
    SehatyValidationError,
)


class PharmacyItemRow(DomainModel):
    """One prescription line as the pharmacy sees it, with the outstanding amount."""

    prescription_item_id: int
    drug: str
    dosage: str
    frequency: str
    quantity: int
    quantity_dispensed: int
    remaining: int


class PharmacyPrescriptionView(DomainModel):
    """A prescription looked up by code, for the dispensing screen."""

    prescription_id: int
    code: str
    status: str
    issued_at: datetime
    expires_at: datetime
    fully_dispensed: bool
    items: list[PharmacyItemRow]


class DispenseItemRow(DomainModel):
    prescription_item_id: int
    drug: str
    quantity: int


class DispenseRow(DomainModel):
    """A recorded dispense (detached)."""

    id: int
    prescription_code: str
    dispensed_at: datetime
    notes: str | None
    items: list[DispenseItemRow]


class StockRow(DomainModel):
    """One medication in a pharmacy's stock (``is_low`` when at/under threshold)."""

    id: int
    medication_id: int
    medication: str
    brand: str | None
    form: str
    quantity: int
    price: float | None
    low_threshold: int
    is_low: bool


class MedicationRow(DomainModel):
    """A catalogue medication, for the add-stock picker."""

    id: int
    name: str
    brand: str | None
    form: str


def _as_utc(dt: datetime) -> datetime:
    """Normalise a (possibly SQLite-naive) datetime to UTC-aware."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _drug_label(session, item: PrescriptionItem) -> str:
    """The item's free-typed drug name, else its linked medication's INN name."""
    if item.drug_name:
        return item.drug_name
    if item.medication_id is not None:
        name = session.execute(
            select(Medication.inn_name).where(Medication.id == item.medication_id)
        ).scalar_one_or_none()
        if name:
            return name
    return "—"


class PharmacyController:
    @staticmethod
    def lookup(code: str) -> PharmacyPrescriptionView:
        """Find a prescription by its public code and show its outstanding lines."""
        code = code.strip().upper()
        with get_session() as session:
            rx = session.execute(
                select(Prescription).where(Prescription.code == code)
            ).scalar_one_or_none()
            if rx is None:
                raise SehatyNotFoundError(f"prescription {code} not found")
            items = PharmacyController._item_rows(session, rx.id)
            return PharmacyPrescriptionView(
                prescription_id=rx.id,
                code=rx.code,
                status=str(rx.status),
                issued_at=rx.issued_at,
                expires_at=rx.expires_at,
                fully_dispensed=bool(items) and all(i.remaining == 0 for i in items),
                items=items,
            )

    @staticmethod
    def _item_rows(session, prescription_id: int) -> list[PharmacyItemRow]:
        rows = session.execute(
            select(
                PrescriptionItem.id,
                PrescriptionItem.drug_name,
                Medication.inn_name,
                PrescriptionItem.dosage,
                PrescriptionItem.frequency,
                PrescriptionItem.quantity,
                PrescriptionItem.quantity_dispensed,
            )
            .outerjoin(Medication, PrescriptionItem.medication_id == Medication.id)
            .where(PrescriptionItem.prescription_id == prescription_id)
            .order_by(PrescriptionItem.id)
        ).all()
        return [
            PharmacyItemRow(
                prescription_item_id=r.id,
                drug=r.drug_name or r.inn_name or "—",
                dosage=r.dosage,
                frequency=r.frequency,
                quantity=r.quantity,
                quantity_dispensed=r.quantity_dispensed,
                remaining=max(0, r.quantity - r.quantity_dispensed),
            )
            for r in rows
        ]

    @staticmethod
    def dispense(
        pharmacy_id: int,
        code: str,
        lines: list[dict],
        notes: str | None = None,
        now: datetime | None = None,
    ) -> DispenseRow:
        """Record a dispense of ``lines`` against the prescription ``code``.

        ``lines`` is ``[{"prescription_item_id": int, "quantity": int}]``. The
        prescription must be ISSUED and not expired; a line may not exceed the
        item's remaining quantity. Updates ``quantity_dispensed``, decrements the
        pharmacy's stock for catalog drugs (when a stock row exists), and writes
        an audit entry.
        """
        now = datetime.now(UTC) if now is None else _as_utc(now)
        code = code.strip().upper()
        wanted = [
            (int(line["prescription_item_id"]), int(line["quantity"]))
            for line in lines
            if int(line.get("quantity", 0)) > 0
        ]
        if not wanted:
            raise SehatyValidationError("no items to dispense")

        with get_session() as session:
            rx = session.execute(
                select(Prescription).where(Prescription.code == code)
            ).scalar_one_or_none()
            if rx is None:
                raise SehatyNotFoundError(f"prescription {code} not found")
            if rx.status != PrescriptionStatus.ISSUED:
                raise SehatyConflictError(f"cannot dispense a {rx.status.value} prescription")
            if _as_utc(rx.expires_at) < now:
                raise SehatyConflictError("prescription has expired")

            items = {
                it.id: it
                for it in session.execute(
                    select(PrescriptionItem).where(PrescriptionItem.prescription_id == rx.id)
                ).scalars()
            }

            dispense = Dispense(
                prescription_id=rx.id, pharmacy_id=pharmacy_id, dispensed_at=now, notes=notes
            )
            session.add(dispense)
            session.flush()

            recorded: list[DispenseItemRow] = []
            for item_id, qty in wanted:
                item = items.get(item_id)
                if item is None:
                    raise SehatyValidationError(f"item {item_id} is not on prescription {code}")
                remaining = item.quantity - item.quantity_dispensed
                if qty > remaining:
                    raise SehatyConflictError(
                        f"cannot dispense {qty} of item {item_id}; only {remaining} remaining"
                    )
                item.quantity_dispensed += qty
                session.add(
                    DispenseItem(
                        dispense_id=dispense.id, prescription_item_id=item_id, quantity=qty
                    )
                )
                if item.medication_id is not None:
                    stock = session.execute(
                        select(PharmacyStock).where(
                            PharmacyStock.pharmacy_id == pharmacy_id,
                            PharmacyStock.medication_id == item.medication_id,
                        )
                    ).scalar_one_or_none()
                    if stock is not None:
                        stock.quantity = max(0, stock.quantity - qty)
                recorded.append(
                    DispenseItemRow(
                        prescription_item_id=item_id,
                        drug=_drug_label(session, item),
                        quantity=qty,
                    )
                )

            session.add(
                AuditLog(
                    actor_user_id=pharmacy_id,
                    action="DISPENSE",
                    entity="prescription",
                    entity_id=rx.id,
                )
            )
            session.flush()
            return DispenseRow(
                id=dispense.id,
                prescription_code=rx.code,
                dispensed_at=dispense.dispensed_at,
                notes=dispense.notes,
                items=recorded,
            )

    @staticmethod
    def list_stock(
        pharmacy_id: int, search: str | None = None, low_only: bool = False
    ) -> list[StockRow]:
        """A pharmacy's medication stock (optionally filtered by name / low-only)."""
        stmt = (
            select(
                PharmacyStock.id,
                PharmacyStock.medication_id,
                Medication.inn_name,
                Medication.brand_name,
                Medication.form,
                PharmacyStock.quantity,
                PharmacyStock.price,
                PharmacyStock.low_threshold,
            )
            .join(Medication, PharmacyStock.medication_id == Medication.id)
            .where(PharmacyStock.pharmacy_id == pharmacy_id)
            .order_by(Medication.inn_name)
        )
        if search and search.strip():
            like = f"%{search.strip()}%"
            stmt = stmt.where(Medication.inn_name.ilike(like) | Medication.brand_name.ilike(like))
        with get_session() as session:
            rows = session.execute(stmt).all()
        out = [
            StockRow(
                id=r.id,
                medication_id=r.medication_id,
                medication=r.inn_name,
                brand=r.brand_name,
                form=r.form,
                quantity=r.quantity,
                price=r.price,
                low_threshold=r.low_threshold,
                is_low=r.quantity <= r.low_threshold,
            )
            for r in rows
        ]
        return [s for s in out if s.is_low] if low_only else out

    @staticmethod
    def search_medications(query: str, limit: int = 20) -> list[MedicationRow]:
        """Search the medication catalogue by INN / brand name (for the picker)."""
        q = (query or "").strip()
        if not q:
            return []
        like = f"%{q}%"
        with get_session() as session:
            rows = session.execute(
                select(Medication.id, Medication.inn_name, Medication.brand_name, Medication.form)
                .where(Medication.inn_name.ilike(like) | Medication.brand_name.ilike(like))
                .order_by(Medication.inn_name)
                .limit(limit)
            ).all()
        return [
            MedicationRow(id=r.id, name=r.inn_name, brand=r.brand_name, form=r.form) for r in rows
        ]

    @staticmethod
    def save_stock(
        pharmacy_id: int,
        medication_id: int,
        quantity: int,
        price: float | None = None,
        low_threshold: int = 10,
    ) -> StockRow:
        """Create or update the stock row for ``(pharmacy, medication)``."""
        if quantity < 0 or low_threshold < 0:
            raise SehatyValidationError("quantity and threshold must be non-negative")
        with get_session() as session:
            med = session.execute(
                select(Medication.inn_name, Medication.brand_name, Medication.form).where(
                    Medication.id == medication_id
                )
            ).one_or_none()
            if med is None:
                raise SehatyNotFoundError(f"medication {medication_id} not found")
            stock = session.execute(
                select(PharmacyStock).where(
                    PharmacyStock.pharmacy_id == pharmacy_id,
                    PharmacyStock.medication_id == medication_id,
                )
            ).scalar_one_or_none()
            if stock is None:
                stock = PharmacyStock(
                    pharmacy_id=pharmacy_id,
                    medication_id=medication_id,
                    quantity=quantity,
                    price=price,
                    low_threshold=low_threshold,
                )
                session.add(stock)
            else:
                stock.quantity = quantity
                stock.price = price
                stock.low_threshold = low_threshold
            session.flush()
            return StockRow(
                id=stock.id,
                medication_id=medication_id,
                medication=med.inn_name,
                brand=med.brand_name,
                form=med.form,
                quantity=quantity,
                price=price,
                low_threshold=low_threshold,
                is_low=quantity <= low_threshold,
            )
