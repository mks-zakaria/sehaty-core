"""PharmacyController tests on an in-memory SQLite engine.

Seeds a prescription with two lines (one catalog-linked with pharmacy stock, one
freehand) and exercises lookup + dispense: outstanding amounts, stock decrement,
over-dispense and non-ISSUED guards.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sehaty.db import (
    AuditLog,
    Dispense,
    DispenseItem,
    Medication,
    PharmacyStock,
    Prescription,
    PrescriptionItem,
    PrescriptionStatus,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.pharmacy import PharmacyController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyConflictError, SehatyNotFoundError

_NOW = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)

_TABLES = [
    User.__table__,
    Medication.__table__,
    Prescription.__table__,
    PrescriptionItem.__table__,
    Dispense.__table__,
    DispenseItem.__table__,
    PharmacyStock.__table__,
    AuditLog.__table__,
]


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _seed(factory, *, status=PrescriptionStatus.ISSUED, expires=_NOW + timedelta(days=30)):
    """Return (pharmacy_id, code, item1_id, item2_id, med_id)."""
    with factory() as s:
        doctor = User(email="d@clinic.ma", role=UserRole.DOCTOR, is_active=True)
        pharmacy = User(email="ph@pharma.ma", role=UserRole.PHARMACY, is_active=True)
        med = Medication(inn_name="Amoxicillin", form="tablet")
        s.add_all([doctor, pharmacy, med])
        s.flush()
        rx = Prescription(
            doctor_id=doctor.id,
            code="RX-P1",
            qr_token="tok-p1",
            status=status,
            issued_at=_NOW,
            expires_at=expires,
        )
        s.add(rx)
        s.flush()
        item1 = PrescriptionItem(
            prescription_id=rx.id,
            medication_id=med.id,
            dosage="1 tab",
            frequency="2x/day",
            quantity=10,
        )
        item2 = PrescriptionItem(
            prescription_id=rx.id,
            drug_name="Paracetamol",
            dosage="1 tab",
            frequency="3x/day",
            quantity=20,
        )
        s.add_all(
            [
                item1,
                item2,
                PharmacyStock(pharmacy_id=pharmacy.id, medication_id=med.id, quantity=100),
            ]
        )
        s.commit()
        return pharmacy.id, rx.code, item1.id, item2.id, med.id


def test_lookup_shows_outstanding(db):
    _pharmacy, code, i1, i2, _med = _seed(db)
    view = PharmacyController.lookup("  rx-p1  ")  # trimmed + upper-cased
    assert view.code == "RX-P1"
    assert not view.fully_dispensed
    by_id = {it.prescription_item_id: it for it in view.items}
    assert by_id[i1].drug == "Amoxicillin" and by_id[i1].remaining == 10
    assert by_id[i2].drug == "Paracetamol" and by_id[i2].remaining == 20


def test_lookup_unknown_code(db):
    _seed(db)
    with pytest.raises(SehatyNotFoundError):
        PharmacyController.lookup("NOPE")


def test_dispense_partial_updates_and_decrements_stock(db):
    pharmacy, code, i1, i2, med = _seed(db)
    result = PharmacyController.dispense(
        pharmacy,
        code,
        [{"prescription_item_id": i1, "quantity": 4}, {"prescription_item_id": i2, "quantity": 5}],
    )
    assert result.prescription_code == "RX-P1"
    assert {(r.prescription_item_id, r.quantity) for r in result.items} == {(i1, 4), (i2, 5)}

    # Outstanding amounts dropped.
    view = PharmacyController.lookup(code)
    by_id = {it.prescription_item_id: it for it in view.items}
    assert by_id[i1].quantity_dispensed == 4 and by_id[i1].remaining == 6
    assert by_id[i2].quantity_dispensed == 5 and by_id[i2].remaining == 15

    # Stock decremented for the catalog-linked item only.
    with db() as s:
        stock = s.execute(
            select(PharmacyStock.quantity).where(
                PharmacyStock.pharmacy_id == pharmacy, PharmacyStock.medication_id == med
            )
        ).scalar_one()
    assert stock == 96


def test_dispense_over_remaining_conflicts(db):
    pharmacy, code, i1, _i2, _med = _seed(db)
    with pytest.raises(SehatyConflictError):
        PharmacyController.dispense(pharmacy, code, [{"prescription_item_id": i1, "quantity": 11}])


def test_cannot_dispense_cancelled(db):
    pharmacy, code, i1, _i2, _med = _seed(db, status=PrescriptionStatus.CANCELLED)
    with pytest.raises(SehatyConflictError):
        PharmacyController.dispense(pharmacy, code, [{"prescription_item_id": i1, "quantity": 1}])


def test_cannot_dispense_expired(db):
    pharmacy, code, i1, _i2, _med = _seed(db, expires=_NOW - timedelta(days=1))
    with pytest.raises(SehatyConflictError):
        PharmacyController.dispense(
            pharmacy, code, [{"prescription_item_id": i1, "quantity": 1}], now=_NOW
        )


def test_stock_list_save_and_search(db):
    pharmacy, _code, _i1, _i2, med = _seed(db)  # seed made stock qty 100 for med

    stock = PharmacyController.list_stock(pharmacy)
    assert len(stock) == 1
    assert stock[0].medication == "Amoxicillin" and stock[0].quantity == 100
    assert not stock[0].is_low

    # Update the same (pharmacy, medication) row -> now low.
    row = PharmacyController.save_stock(pharmacy, med, quantity=5, price=12.5, low_threshold=10)
    assert row.quantity == 5 and row.price == 12.5 and row.is_low
    assert len(PharmacyController.list_stock(pharmacy, low_only=True)) == 1

    # Catalogue search.
    assert any(m.id == med for m in PharmacyController.search_medications("amox"))
    assert PharmacyController.search_medications("  ") == []


def test_save_stock_creates_new_row(db):
    pharmacy, *_ = _seed(db)
    with db() as s:
        m2 = Medication(inn_name="Ibuprofen", form="tablet")
        s.add(m2)
        s.commit()
        m2_id = m2.id
    row = PharmacyController.save_stock(pharmacy, m2_id, quantity=50)
    assert row.medication == "Ibuprofen" and row.quantity == 50
    assert len(PharmacyController.list_stock(pharmacy)) == 2
