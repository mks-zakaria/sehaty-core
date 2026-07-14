"""Doctor-referral core tests on an in-memory SQLite engine.

Mirrors ``test_admin.py``: the PostGIS ``Geography`` column on
``DoctorProfile`` is not buildable on stock SQLite, so this module registers
dialect-scoped compilation shims (geo type -> ``TEXT``) purely for the test
engine. The referral controller only ever projects/updates plain columns, so
``geopoint`` is never selected.

Covers code minting (stable + unique), referrer resolution round-trip, referral
recording rules (self-referral + duplicate guards), the full first-paid-invoice
reward flow, settlement idempotency, and the admin-config reward override.
"""

import json
from datetime import UTC, datetime

import pytest
from geoalchemy2 import Geography
from sehaty.db import (
    AdminConfig,
    AuditLog,
    CreditLedger,
    DoctorProfile,
    Invoice,
    Payment,
    Plan,
    Referral,
    ReferralStatus,
    Subscription,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.billing import BillingController
from sehaty.core.controllers.referral import ReferralController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyNotFoundError


@compiles(Geography, "sqlite")
def _geography_as_text_on_sqlite(element, compiler, **kw) -> str:
    # SQLite has no geography type; store the column as opaque TEXT for tests.
    return "TEXT"


_TABLES = [
    User.__table__,
    DoctorProfile.__table__,
    Referral.__table__,
    AdminConfig.__table__,
    Plan.__table__,
    Subscription.__table__,
    Invoice.__table__,
    Payment.__table__,
    CreditLedger.__table__,
    AuditLog.__table__,
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


def _seed_doctor(factory: sessionmaker[Session], *, email: str, license_no: str) -> int:
    """Create a DOCTOR user + profile and return the shared user_id."""
    with factory() as s:
        user = User(email=email, role=UserRole.DOCTOR, is_active=True)
        s.add(user)
        s.flush()
        s.add(
            DoctorProfile(
                user_id=user.id,
                full_name="Dr Test",
                slug=f"dr-{license_no.lower()}",
                license_no=license_no,
                city="Casablanca",
            )
        )
        s.commit()
        return user.id


def _seed_admin(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        user = User(email="admin@sehaty.ma", role=UserRole.ADMIN, is_active=True)
        s.add(user)
        s.commit()
        return user.id


# --------------------------------------------------------------------------- #
# Codes + resolution
# --------------------------------------------------------------------------- #


def test_code_for_is_stable_and_resolves(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, email="a@clinic.ma", license_no="LIC-A")

    code = ReferralController.code_for(doc)
    assert code
    # Stable: a second call returns the very same persisted code.
    assert ReferralController.code_for(doc) == code
    # Round-trips back to the owning doctor.
    assert ReferralController.resolve_referrer(code) == doc
    assert ReferralController.resolve_referrer("NOPE") is None


def test_code_for_is_unique_across_doctors(db: sessionmaker[Session]) -> None:
    d1 = _seed_doctor(db, email="a@clinic.ma", license_no="LIC-A")
    d2 = _seed_doctor(db, email="b@clinic.ma", license_no="LIC-B")
    assert ReferralController.code_for(d1) != ReferralController.code_for(d2)


def test_code_for_missing_profile_raises(db: sessionmaker[Session]) -> None:
    with pytest.raises(SehatyNotFoundError):
        ReferralController.code_for(999999)


# --------------------------------------------------------------------------- #
# Recording referrals
# --------------------------------------------------------------------------- #


def test_record_referral_links_referrer_to_referred(db: sessionmaker[Session]) -> None:
    referrer = _seed_doctor(db, email="ref@clinic.ma", license_no="LIC-R")
    referred = _seed_doctor(db, email="new@clinic.ma", license_no="LIC-N")
    code = ReferralController.code_for(referrer)

    referral = ReferralController.record_referral(code, referred)
    assert referral is not None
    assert referral.referrer_doctor_id == referrer
    assert referral.referred_doctor_id == referred
    assert referral.status == ReferralStatus.PENDING

    assert [r.referred_doctor_id for r in ReferralController.list_for_referrer(referrer)] == [
        referred
    ]


def test_record_referral_rejects_self_referral(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db, email="self@clinic.ma", license_no="LIC-S")
    code = ReferralController.code_for(doc)
    assert ReferralController.record_referral(code, doc) is None


def test_record_referral_no_duplicate_for_same_referred(db: sessionmaker[Session]) -> None:
    r1 = _seed_doctor(db, email="r1@clinic.ma", license_no="LIC-1")
    r2 = _seed_doctor(db, email="r2@clinic.ma", license_no="LIC-2")
    referred = _seed_doctor(db, email="new@clinic.ma", license_no="LIC-N")

    assert ReferralController.record_referral(ReferralController.code_for(r1), referred) is not None
    # Already claimed: a second code cannot re-refer the same doctor.
    assert ReferralController.record_referral(ReferralController.code_for(r2), referred) is None

    with db() as s:
        rows = (
            s.execute(select(Referral).where(Referral.referred_doctor_id == referred))
            .scalars()
            .all()
        )
    assert len(rows) == 1


def test_record_referral_unknown_code_returns_none(db: sessionmaker[Session]) -> None:
    referred = _seed_doctor(db, email="new@clinic.ma", license_no="LIC-N")
    assert ReferralController.record_referral("GHOSTCODE", referred) is None


# --------------------------------------------------------------------------- #
# Reward amount config
# --------------------------------------------------------------------------- #


def test_reward_amount_defaults_to_199(db: sessionmaker[Session]) -> None:
    assert ReferralController.reward_amount() == 199.0


def test_reward_amount_respects_admin_override(db: sessionmaker[Session]) -> None:
    with db() as s:
        s.add(AdminConfig(key="referral_reward_mad", value=json.dumps(250.0)))
        s.commit()
    assert ReferralController.reward_amount() == 250.0


# --------------------------------------------------------------------------- #
# Full flow + idempotency
# --------------------------------------------------------------------------- #


def _pay_first_invoice(
    db: sessionmaker[Session], admin: int, referred: int, receipt_no: str
) -> tuple[int, Payment]:
    """Subscribe the referred doctor, invoice, and record a cash payment."""
    BillingController.subscribe(referred, "essentiel")
    invoice = BillingController.generate_invoice(referred)
    payment = BillingController.record_cash_payment(
        admin, invoice.id, invoice.amount, receipt_no=receipt_no, paid_at=_NOW
    )
    return invoice.id, payment


def test_first_paid_invoice_rewards_referrer(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    admin = _seed_admin(db)
    referrer = _seed_doctor(db, email="ref@clinic.ma", license_no="LIC-R")
    referred = _seed_doctor(db, email="new@clinic.ma", license_no="LIC-N")
    ReferralController.record_referral(ReferralController.code_for(referrer), referred)

    _pay_first_invoice(db, admin, referred, "R-001")

    # Default 199 MAD lands on the referrer's ledger.
    assert BillingController.credit_balance(referrer) == 199.0
    with db() as s:
        referral = s.execute(
            select(Referral).where(Referral.referred_doctor_id == referred)
        ).scalar_one()
    assert referral.status == ReferralStatus.REWARDED
    assert referral.reward_amount == 199.0
    assert referral.credit_ledger_id is not None


def test_reward_uses_admin_override_amount(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    admin = _seed_admin(db)
    referrer = _seed_doctor(db, email="ref@clinic.ma", license_no="LIC-R")
    referred = _seed_doctor(db, email="new@clinic.ma", license_no="LIC-N")
    with db() as s:
        s.add(AdminConfig(key="referral_reward_mad", value=json.dumps(300.0)))
        s.commit()
    ReferralController.record_referral(ReferralController.code_for(referrer), referred)

    _pay_first_invoice(db, admin, referred, "R-002")
    assert BillingController.credit_balance(referrer) == 300.0


def test_settle_is_idempotent_no_double_credit(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    admin = _seed_admin(db)
    referrer = _seed_doctor(db, email="ref@clinic.ma", license_no="LIC-R")
    referred = _seed_doctor(db, email="new@clinic.ma", license_no="LIC-N")
    ReferralController.record_referral(ReferralController.code_for(referrer), referred)

    invoice_id, _ = _pay_first_invoice(db, admin, referred, "R-003")
    assert BillingController.credit_balance(referrer) == 199.0

    # Replaying the same receipt is a no-op (returns the existing payment).
    BillingController.record_cash_payment(
        admin, invoice_id, 199.0, receipt_no="R-003", paid_at=_NOW
    )
    # Calling settle directly again also does nothing (referral already REWARDED).
    assert ReferralController.settle_first_payment(referred) == 0.0
    assert BillingController.credit_balance(referrer) == 199.0


def test_no_referral_means_no_reward(db: sessionmaker[Session]) -> None:
    BillingController.seed_plans()
    admin = _seed_admin(db)
    referred = _seed_doctor(db, email="solo@clinic.ma", license_no="LIC-N")

    # No referral recorded: settlement credits nobody.
    _pay_first_invoice(db, admin, referred, "R-004")
    assert ReferralController.settle_first_payment(referred) == 0.0
