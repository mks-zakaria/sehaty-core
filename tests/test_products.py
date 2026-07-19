"""ProductController + SaleController tests on an in-memory SQLite engine.

Registers a medicine and a cosmetic, looks them up by barcode, sells a mixed
basket (stock decremented, total computed, receipt lines returned) and guards
over-selling / unknown barcodes.
"""

from datetime import UTC, datetime

import pytest
from sehaty.db import PharmacyProduct, Sale, SaleItem, User, UserRole
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.products import ProductController, SaleController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyNotFoundError,
    SehatyValidationError,
)

_NOW = datetime(2026, 7, 19, 10, 0, tzinfo=UTC)

_TABLES = [
    User.__table__,
    PharmacyProduct.__table__,
    Sale.__table__,
    SaleItem.__table__,
]


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    with factory() as s:
        s.add(User(id=1, email="ph@pharma.ma", role=UserRole.PHARMACY, is_active=True))
        s.commit()
    yield factory
    session_mod.set_session_factory(None)


def test_register_lookup_and_sell(db):
    ProductController.register(1, "6111", "Doliprane 1000", "MEDICINE", price=20.0, quantity=5)
    ProductController.register(1, "6222", "Nivea Cream", "COSMETIC", price=35.0, quantity=3)

    found = ProductController.lookup(1, "6111")
    assert found.name == "Doliprane 1000"
    assert found.kind == "MEDICINE"

    sale = SaleController.sell(
        1,
        [{"barcode": "6111", "quantity": 2}, {"barcode": "6222", "quantity": 1}],
        now=_NOW,
    )
    assert sale.total == pytest.approx(20.0 * 2 + 35.0)
    assert len(sale.items) == 2

    # stock decremented
    assert ProductController.lookup(1, "6111").quantity == 3
    assert ProductController.lookup(1, "6222").quantity == 2

    history = SaleController.list_sales(1)
    assert len(history) == 1
    assert history[0].total == pytest.approx(75.0)


def test_register_is_upsert_by_barcode(db):
    ProductController.register(1, "6111", "Old", "MEDICINE", price=10.0, quantity=5)
    row = ProductController.register(1, "6111", "New Name", "MEDICINE", price=12.0, quantity=8)
    assert row.name == "New Name"
    assert row.price == 12.0
    assert len(ProductController.list_products(1)) == 1


def test_low_stock_flag_and_filter(db):
    ProductController.register(1, "A", "Low One", "COSMETIC", quantity=2, low_threshold=5)
    ProductController.register(1, "B", "Plenty", "COSMETIC", quantity=50, low_threshold=5)
    low = ProductController.list_products(1, low_only=True)
    assert [r.name for r in low] == ["Low One"]


def test_restock_adds_quantity(db):
    p = ProductController.register(1, "A", "Widget", "COSMETIC", quantity=1)
    updated = ProductController.restock(1, p.id, 10)
    assert updated.quantity == 11


def test_sell_over_stock_raises(db):
    ProductController.register(1, "A", "Scarce", "MEDICINE", price=5.0, quantity=1)
    with pytest.raises(SehatyConflictError):
        SaleController.sell(1, [{"barcode": "A", "quantity": 5}], now=_NOW)


def test_lookup_unknown_barcode_raises(db):
    with pytest.raises(SehatyNotFoundError):
        ProductController.lookup(1, "nope")


def test_register_bad_kind_raises(db):
    with pytest.raises(SehatyValidationError):
        ProductController.register(1, "A", "Thing", "FOOD")
