"""Cash-billing core tests on an in-memory SQLite engine.

Covers plan seeding idempotency, subscribing, invoice generation with credit
offset, idempotent cash-receipt recording, and the dunning sweep. Only the
tables this feature touches (users, plans, subscriptions, invoices, payments,
credit_ledger, audit_logs, plus referrals + admin_config, which
``record_cash_payment`` reads when settling a first-paid-invoice referral) are
created — none carry the PostGIS ``geopoint`` column, so no dialect shims are
needed.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sehaty.db import (
    AdminConfig,
    AuditLog,
    CreditLedger,
    Invoice,
    InvoiceStatus,
    Payment,
    Plan,
    Referral,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.billing import BillingController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyForbiddenError, SehatyNotFoundError

_TABLES = [
    User.__table__,
    Plan.__table__,
    Subscription.__table__,
    Invoice.__table__,
    Payment.__table__,
    CreditLedger.__table__,
    AuditLog.__table__,
    # record_cash_payment settles referrals on the first paid invoice, so it
    # reads these tables (there are simply no referrals in the billing suite).
    Referral.__table__,
    AdminConfig.__table__,
]

_NOW = datetime(2026, 7, 13, 10, 0, tzinfo=UTC)


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _seed_user(factory: sessionmaker[Session], *, email: str, role: UserRole) -> int:
    with factory() as s:
        user = User(email=email, role=role, is_active=True)
        s.add(user)
        s.commit()
        return user.id


def _seed_doctor(factory: sessionmaker[Session], email: str = "doc@clinic.ma") -> int:
    return _seed_user(factory, email=email, role=UserRole.DOCTOR)


def _seed_admin(factory: sessionmaker[Session], email: str = "admin@sehaty.ma") -> int:
    return _seed_user(factory, email=email, role=UserRole.ADMIN)


# --------------------------------------------------------------------------- #
# Plan catalogue
# --------------------------------------------------------------------------- #


def test_seed_plans_is_idempotent(db: sessionmaker[Session]) -> None:
    first = BillingController.seed_plans()
    assert {p.code for p in first} == {"essentiel", "pro", "premium"}

    # Second call adds nothing.
    second = BillingController.seed_plans()
    assert second == []

    plans = BillingController.list_plans()
    assert len(plans) == 3
    # Ordered by monthly price.
    assert [p.code for p in plans] == ["essentiel", "pro", "premium"]
    assert [p.price_month for p in plans] == [199.0, 299.0, 499.0]
    assert all(p.currency == "MAD" and p.is_active for p in plans)


# --------------------------------------------------------------------------- #
# Subscribe
# --------------------------------------------------------------------------- #


def test_subscribe_sets_active(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    doc = _seed_doctor(db)

    sub = BillingController.subscribe(doc, "pro")
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.current_period_end > sub.current_period_start

    with db() as s:
        rows = s.execute(select(Subscription).where(Subscription.doctor_id == doc)).scalars().all()
    assert len(rows) == 1


def test_subscribe_replaces_existing_subscription(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    doc = _seed_doctor(db)

    BillingController.subscribe(doc, "essentiel")
    BillingController.subscribe(doc, "premium")

    with db() as s:
        rows = s.execute(select(Subscription).where(Subscription.doctor_id == doc)).scalars().all()
    # Still a single subscription, re-pointed at the new plan.
    assert len(rows) == 1
    premium = s.execute(select(Plan).where(Plan.code == "premium")).scalar_one()
    assert rows[0].plan_id == premium.id


def test_subscribe_unknown_plan_raises(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    doc = _seed_doctor(db)
    with pytest.raises(SehatyNotFoundError):
        BillingController.subscribe(doc, "enterprise")


# --------------------------------------------------------------------------- #
# Invoicing + credit
# --------------------------------------------------------------------------- #


def test_generate_invoice_amount_is_plan_price(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    doc = _seed_doctor(db)
    BillingController.subscribe(doc, "pro")

    invoice = BillingController.generate_invoice(doc)
    assert invoice.amount == 299.0
    assert invoice.currency == "MAD"
    assert invoice.status == InvoiceStatus.OPEN


def test_credit_reduces_amount_due(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    doc = _seed_doctor(db)
    BillingController.subscribe(doc, "pro")

    BillingController.apply_credit(doc, 50.0, reason="referral")
    assert BillingController.credit_balance(doc) == 50.0

    invoice = BillingController.generate_invoice(doc)
    assert invoice.amount == 249.0
    # The applied credit is consumed, not re-usable on the next invoice.
    assert BillingController.credit_balance(doc) == 0.0


def test_credit_over_price_floors_invoice_at_zero(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    doc = _seed_doctor(db)
    BillingController.subscribe(doc, "essentiel")

    BillingController.apply_credit(doc, 500.0, reason="promo")
    invoice = BillingController.generate_invoice(doc)
    assert invoice.amount == 0.0
    # 199 consumed of the 500 credit; the remainder survives.
    assert BillingController.credit_balance(doc) == 301.0


def test_generate_invoice_without_subscription_raises(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyNotFoundError):
        BillingController.generate_invoice(doc)


# --------------------------------------------------------------------------- #
# Cash payments
# --------------------------------------------------------------------------- #


def test_record_cash_payment_marks_paid_and_audits(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    doc = _seed_doctor(db)
    admin = _seed_admin(db)
    BillingController.subscribe(doc, "pro")
    invoice = BillingController.generate_invoice(doc)

    payment = BillingController.record_cash_payment(
        admin, invoice.id, 299.0, receipt_no="R-001", paid_at=_NOW
    )
    assert payment.method == "CASH"
    assert payment.provider_ref == "R-001"

    with db() as s:
        inv = s.get(Invoice, invoice.id)
        assert inv.status == InvoiceStatus.PAID
        assert inv.paid_at is not None
        log = s.execute(select(AuditLog).where(AuditLog.entity_id == invoice.id)).scalar_one()
        assert log.action == "PAYMENT_CASH"
        assert log.actor_user_id == admin


def test_record_cash_payment_is_idempotent_on_receipt_no(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    doc = _seed_doctor(db)
    admin = _seed_admin(db)
    BillingController.subscribe(doc, "pro")
    invoice = BillingController.generate_invoice(doc)

    p1 = BillingController.record_cash_payment(
        admin, invoice.id, 299.0, receipt_no="R-042", paid_at=_NOW
    )
    # Re-entering the same paper receipt is a no-op, not a second charge.
    p2 = BillingController.record_cash_payment(
        admin, invoice.id, 299.0, receipt_no="R-042", paid_at=_NOW + timedelta(hours=1)
    )
    assert p1.id == p2.id

    with db() as s:
        payments = s.execute(select(Payment).where(Payment.provider_ref == "R-042")).scalars().all()
    assert len(payments) == 1


def test_record_cash_payment_requires_admin(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    doc = _seed_doctor(db)
    BillingController.subscribe(doc, "pro")
    invoice = BillingController.generate_invoice(doc)

    with pytest.raises(SehatyForbiddenError):
        BillingController.record_cash_payment(
            doc, invoice.id, 299.0, receipt_no="R-999", paid_at=_NOW
        )


# --------------------------------------------------------------------------- #
# Dunning
# --------------------------------------------------------------------------- #


def test_run_dunning_flips_overdue_subscription_past_due(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    doc = _seed_doctor(db)
    BillingController.subscribe(doc, "pro")
    invoice = BillingController.generate_invoice(doc)

    # Force this invoice overdue.
    with db() as s:
        inv = s.get(Invoice, invoice.id)
        inv.due_at = datetime.now(UTC) - timedelta(days=1)
        s.commit()

    count = BillingController.run_dunning()
    assert count == 1

    status = BillingController.subscription_status(doc)
    assert status.status == SubscriptionStatus.PAST_DUE
    assert status.plan == "pro"
    assert status.amount_due == 299.0


def test_run_dunning_ignores_paid_and_current(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    doc = _seed_doctor(db)
    admin = _seed_admin(db)
    BillingController.subscribe(doc, "pro")
    invoice = BillingController.generate_invoice(doc)
    BillingController.record_cash_payment(
        admin, invoice.id, 299.0, receipt_no="R-100", paid_at=_NOW
    )

    # Paid invoice (not OPEN) and a current-period invoice must be ignored.
    assert BillingController.run_dunning() == 0
    status = BillingController.subscription_status(doc)
    assert status.status == SubscriptionStatus.ACTIVE
    assert status.amount_due == 0.0
