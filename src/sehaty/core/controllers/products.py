"""Pharmacy point-of-sale: product registry + sales.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A pharmacy
registers its over-the-counter products (each with a barcode/QR, a name and a
kind — MEDICINE or COSMETIC), then sells them by scanning the barcode. Selling
records a :class:`Sale` (+ :class:`SaleItem` lines, snapshotting name/price) and
decrements product stock. All reads return detached ``DomainModel`` projections;
failures raise the ``SehatyError`` taxonomy.
"""

from datetime import datetime

from sehaty.db import PharmacyProduct, ProductKind, Sale, SaleItem
from sqlalchemy import select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyNotFoundError,
    SehatyValidationError,
)


class ProductRow(DomainModel):
    id: int
    barcode: str
    name: str
    kind: str
    medication_id: int | None
    price: float | None
    quantity: int
    low_threshold: int
    is_active: bool
    is_low: bool


class SaleItemRow(DomainModel):
    product_id: int | None
    name: str
    quantity: int
    unit_price: float
    line_total: float


class SaleRow(DomainModel):
    id: int
    sold_at: datetime
    total: float
    items: list[SaleItemRow]


def _row(p: PharmacyProduct) -> ProductRow:
    return ProductRow(
        id=p.id,
        barcode=p.barcode,
        name=p.name,
        kind=str(p.kind),
        medication_id=p.medication_id,
        price=p.price,
        quantity=p.quantity,
        low_threshold=p.low_threshold,
        is_active=p.is_active,
        is_low=p.quantity <= p.low_threshold,
    )


class ProductController:
    @staticmethod
    def register(
        pharmacy_id: int,
        barcode: str,
        name: str,
        kind: str,
        medication_id: int | None = None,
        price: float | None = None,
        quantity: int = 0,
        low_threshold: int = 10,
    ) -> ProductRow:
        """Create or update (by barcode) a product in the pharmacy's catalogue."""
        barcode = (barcode or "").strip()
        name = (name or "").strip()
        if not barcode or not name:
            raise SehatyValidationError("barcode and name are required")
        try:
            product_kind = ProductKind(kind)
        except ValueError as exc:
            raise SehatyValidationError(f"invalid product kind: {kind!r}") from exc
        if quantity < 0 or low_threshold < 0:
            raise SehatyValidationError("quantity and threshold must be non-negative")

        with get_session() as session:
            product = session.execute(
                select(PharmacyProduct).where(
                    PharmacyProduct.pharmacy_id == pharmacy_id,
                    PharmacyProduct.barcode == barcode,
                )
            ).scalar_one_or_none()
            if product is None:
                product = PharmacyProduct(pharmacy_id=pharmacy_id, barcode=barcode)
                session.add(product)
            product.name = name
            product.kind = product_kind
            product.medication_id = medication_id
            product.price = price
            product.quantity = quantity
            product.low_threshold = low_threshold
            product.is_active = True
            session.flush()
            return _row(product)

    @staticmethod
    def lookup(pharmacy_id: int, barcode: str) -> ProductRow:
        """Find a product by barcode (the sell scan). Raises if unknown."""
        barcode = (barcode or "").strip()
        with get_session() as session:
            product = session.execute(
                select(PharmacyProduct).where(
                    PharmacyProduct.pharmacy_id == pharmacy_id,
                    PharmacyProduct.barcode == barcode,
                )
            ).scalar_one_or_none()
            if product is None:
                raise SehatyNotFoundError(f"no product with barcode {barcode}")
            return _row(product)

    @staticmethod
    def list_products(
        pharmacy_id: int, search: str | None = None, low_only: bool = False
    ) -> list[ProductRow]:
        """The pharmacy's products, optionally filtered by name/barcode and low-only."""
        stmt = (
            select(PharmacyProduct)
            .where(PharmacyProduct.pharmacy_id == pharmacy_id)
            .order_by(PharmacyProduct.name)
        )
        if search and search.strip():
            like = f"%{search.strip()}%"
            stmt = stmt.where(
                PharmacyProduct.name.ilike(like) | PharmacyProduct.barcode.ilike(like)
            )
        with get_session() as session:
            rows = [_row(p) for p in session.execute(stmt).scalars()]
        return [r for r in rows if r.is_low] if low_only else rows

    @staticmethod
    def restock(pharmacy_id: int, product_id: int, add: int) -> ProductRow:
        """Add received stock to a product (a positive movement)."""
        if add <= 0:
            raise SehatyValidationError("restock amount must be positive")
        with get_session() as session:
            product = session.get(PharmacyProduct, product_id)
            if product is None or product.pharmacy_id != pharmacy_id:
                raise SehatyNotFoundError(f"product {product_id} not found")
            product.quantity += add
            session.flush()
            return _row(product)


class SaleController:
    @staticmethod
    def sell(pharmacy_id: int, lines: list[dict], now: datetime | None = None) -> SaleRow:
        """Record a sale of ``lines`` and decrement product stock.

        Each line is ``{"product_id": int} | {"barcode": str}`` plus ``"quantity"``.
        Fails if a product is unknown/foreign or there isn't enough stock. Returns
        the recorded sale (for the receipt).
        """
        wanted = [
            (
                line.get("product_id"),
                (line.get("barcode") or "").strip(),
                int(line.get("quantity", 0)),
            )
            for line in lines
        ]
        wanted = [(pid, bc, qty) for pid, bc, qty in wanted if qty > 0]
        if not wanted:
            raise SehatyValidationError("no items to sell")

        with get_session() as session:
            sale = Sale(pharmacy_id=pharmacy_id, total=0.0)
            if now is not None:
                sale.sold_at = now
            session.add(sale)
            session.flush()

            total = 0.0
            recorded: list[SaleItemRow] = []
            for product_id, barcode, qty in wanted:
                if product_id is not None:
                    product = session.get(PharmacyProduct, product_id)
                else:
                    product = session.execute(
                        select(PharmacyProduct).where(
                            PharmacyProduct.pharmacy_id == pharmacy_id,
                            PharmacyProduct.barcode == barcode,
                        )
                    ).scalar_one_or_none()
                if product is None or product.pharmacy_id != pharmacy_id:
                    raise SehatyNotFoundError(f"product not found: {product_id or barcode}")
                if product.quantity < qty:
                    raise SehatyConflictError(
                        f"not enough stock for {product.name}: {product.quantity} left"
                    )
                unit_price = float(product.price or 0.0)
                line_total = unit_price * qty
                total += line_total
                product.quantity -= qty
                session.add(
                    SaleItem(
                        sale_id=sale.id,
                        product_id=product.id,
                        name=product.name,
                        quantity=qty,
                        unit_price=unit_price,
                        line_total=line_total,
                    )
                )
                recorded.append(
                    SaleItemRow(
                        product_id=product.id,
                        name=product.name,
                        quantity=qty,
                        unit_price=unit_price,
                        line_total=line_total,
                    )
                )

            sale.total = total
            session.flush()
            return SaleRow(id=sale.id, sold_at=sale.sold_at, total=total, items=recorded)

    @staticmethod
    def list_sales(pharmacy_id: int, limit: int = 50) -> list[SaleRow]:
        """Recent sales (newest first) with their lines — the sales history."""
        with get_session() as session:
            sales = session.execute(
                select(Sale)
                .where(Sale.pharmacy_id == pharmacy_id)
                .order_by(Sale.sold_at.desc(), Sale.id.desc())
                .limit(limit)
            ).scalars().all()
            out: list[SaleRow] = []
            for sale in sales:
                items = session.execute(
                    select(SaleItem).where(SaleItem.sale_id == sale.id).order_by(SaleItem.id)
                ).scalars()
                out.append(
                    SaleRow(
                        id=sale.id,
                        sold_at=sale.sold_at,
                        total=sale.total,
                        items=[
                            SaleItemRow(
                                product_id=it.product_id,
                                name=it.name,
                                quantity=it.quantity,
                                unit_price=it.unit_price,
                                line_total=it.line_total,
                            )
                            for it in items
                        ],
                    )
                )
            return out
