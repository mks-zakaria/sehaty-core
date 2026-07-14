"""Reporting + year-end accounting tests on an in-memory SQLite engine.

Covers the operational KPI snapshot, the fiscal-year revenue summary (cash
buckets + MRR), and the immutable accounting export. The doctor-profile table
carries the PostGIS ``geopoint`` column, which stock SQLite cannot compile, so
a tiny ``Geography -> TEXT`` compiler shim is registered for the ``sqlite``
dialect — the reporting queries never touch ``geopoint`` itself, only the
verification status alongside it.
"""

from datetime import UTC, datetime

import pytest
from geoalchemy2 import Geography
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    CreditLedger,
    DoctorProfile,
    Invoice,
    InvoiceStatus,
    PatientProfile,
    Payment,
    Plan,
    Referral,
    ReferralStatus,
    Review,
    ReviewDirection,
    ReviewStatus,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
    VerificationStatus,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, event
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.reporting import ReportingController
from sehaty.core.db import session as session_mod


@compiles(Geography, "sqlite")
def _compile_geography_sqlite(type_, compiler, **kw) -> str:  # noqa: ANN001
    """Render the PostGIS ``geography`` column as TEXT so SQLite can build it."""
    return "TEXT"


_TABLES = [
    User.__table__,
    DoctorProfile.__table__,
    PatientProfile.__table__,
    Appointment.__table__,
    Review.__table__,
    Referral.__table__,
    Plan.__table__,
    Subscription.__table__,
    Invoice.__table__,
    Payment.__table__,
    CreditLedger.__table__,
]

_YEAR = 2026


def _dt(month: int, day: int = 15, *, year: int = _YEAR) -> datetime:
    return datetime(year, month, day, 10, 0, tzinfo=UTC)


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # GeoAlchemy2 wraps geopoint writes in ST_GeogFromText(); register a no-op
    # SQLite UDF so inserts of NULL geopoints (the reporting suite never sets
    # one) round-trip without a live PostGIS.
    @event.listens_for(engine, "connect")
    def _register_geog_udf(dbapi_conn, _record) -> None:  # noqa: ANN001
        dbapi_conn.create_function("ST_GeogFromText", 1, lambda value: value)

    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> dict[str, int]:
    """A small fixture spanning every table the reporting queries read."""
    ids: dict[str, int] = {}
    with db() as s:
        # Two doctors (one verified), one patient.
        doc1 = User(email="doc1@clinic.ma", role=UserRole.DOCTOR, is_active=True)
        doc2 = User(email="doc2@clinic.ma", role=UserRole.DOCTOR, is_active=True)
        patient = User(email="pat@sehaty.ma", role=UserRole.PATIENT, is_active=True)
        s.add_all([doc1, doc2, patient])
        s.flush()
        ids.update(doc1=doc1.id, doc2=doc2.id, patient=patient.id)

        s.add_all(
            [
                DoctorProfile(
                    user_id=doc1.id,
                    full_name="Dr Amine",
                    slug="dr-amine",
                    license_no="LIC-1",
                    verification_status=VerificationStatus.VERIFIED,
                ),
                DoctorProfile(
                    user_id=doc2.id,
                    full_name="Dr Sara",
                    slug="dr-sara",
                    license_no="LIC-2",
                    verification_status=VerificationStatus.PENDING,
                ),
                PatientProfile(user_id=patient.id, full_name="Youssef"),
            ]
        )

        # Appointments in mixed statuses.
        for st in (
            AppointmentStatus.COMPLETED,
            AppointmentStatus.COMPLETED,
            AppointmentStatus.CONFIRMED,
            AppointmentStatus.CANCELLED,
        ):
            s.add(
                Appointment(
                    patient_id=patient.id,
                    doctor_id=doc1.id,
                    start_at=_dt(3),
                    end_at=_dt(3),
                    status=st,
                )
            )
        s.flush()
        appts = s.query(Appointment).order_by(Appointment.id).all()
        appt = appts[0]

        # Two published reviews + one pending (not counted).
        s.add_all(
            [
                Review(
                    author_id=patient.id,
                    target_id=doc1.id,
                    appointment_id=appt.id,
                    direction=ReviewDirection.PATIENT_ON_DOCTOR,
                    stars=5,
                    status=ReviewStatus.PUBLISHED,
                ),
                Review(
                    author_id=doc1.id,
                    target_id=patient.id,
                    appointment_id=appt.id,
                    direction=ReviewDirection.DOCTOR_ON_PATIENT,
                    stars=4,
                    status=ReviewStatus.PUBLISHED,
                ),
                Review(
                    author_id=patient.id,
                    target_id=doc2.id,
                    appointment_id=appts[1].id,
                    direction=ReviewDirection.PATIENT_ON_DOCTOR,
                    stars=3,
                    status=ReviewStatus.PENDING,
                ),
            ]
        )

        # One rewarded referral + one pending (not counted).
        s.add_all(
            [
                Referral(
                    referrer_doctor_id=doc1.id,
                    referred_doctor_id=doc2.id,
                    code="REF-1",
                    status=ReferralStatus.REWARDED,
                    reward_amount=199.0,
                ),
                Referral(
                    referrer_doctor_id=doc2.id,
                    code="REF-2",
                    status=ReferralStatus.PENDING,
                ),
            ]
        )

        # Plans + subscriptions (two active, one cancelled -> MRR = 199 + 299).
        p_ess = Plan(code="essentiel", name="Essentiel", price_month=199.0, currency="MAD")
        p_pro = Plan(code="pro", name="Pro", price_month=299.0, currency="MAD")
        s.add_all([p_ess, p_pro])
        s.flush()
        s.add_all(
            [
                Subscription(
                    doctor_id=doc1.id,
                    plan_id=p_ess.id,
                    status=SubscriptionStatus.ACTIVE,
                    current_period_start=_dt(1),
                    current_period_end=_dt(2),
                ),
                Subscription(
                    doctor_id=doc2.id,
                    plan_id=p_pro.id,
                    status=SubscriptionStatus.ACTIVE,
                    current_period_start=_dt(1),
                    current_period_end=_dt(2),
                ),
            ]
        )

        # Invoices: two issued in the target year, one in the prior year.
        inv_feb = Invoice(
            doctor_id=doc1.id,
            amount=199.0,
            currency="MAD",
            status=InvoiceStatus.PAID,
            issued_at=_dt(2),
            due_at=_dt(3),
            paid_at=_dt(2),
        )
        inv_may = Invoice(
            doctor_id=doc2.id,
            amount=299.0,
            currency="MAD",
            status=InvoiceStatus.PAID,
            issued_at=_dt(5),
            due_at=_dt(6),
            paid_at=_dt(5),
        )
        inv_prior = Invoice(
            doctor_id=doc1.id,
            amount=199.0,
            currency="MAD",
            status=InvoiceStatus.PAID,
            issued_at=_dt(11, year=_YEAR - 1),
            due_at=_dt(12, year=_YEAR - 1),
            paid_at=_dt(11, year=_YEAR - 1),
        )
        s.add_all([inv_feb, inv_may, inv_prior])
        s.flush()
        ids.update(inv_feb=inv_feb.id, inv_may=inv_may.id, inv_prior=inv_prior.id)

        # Cash payments: Feb + May of the target year, and one in the prior year.
        s.add_all(
            [
                Payment(
                    invoice_id=inv_feb.id,
                    amount=199.0,
                    method="CASH",
                    provider_ref="R-FEB",
                    paid_at=_dt(2),
                ),
                Payment(
                    invoice_id=inv_may.id,
                    amount=299.0,
                    method="CASH",
                    provider_ref="R-MAY",
                    paid_at=_dt(5),
                ),
                Payment(
                    invoice_id=inv_prior.id,
                    amount=199.0,
                    method="CASH",
                    provider_ref="R-PRIOR",
                    paid_at=_dt(11, year=_YEAR - 1),
                ),
            ]
        )

        # Credit ledger: a referral reward in-year and an offset in the prior year.
        s.add_all(
            [
                CreditLedger(
                    doctor_id=doc1.id,
                    amount=199.0,
                    reason="referral",
                    ref_type="referral",
                    ref_id=1,
                    created_at=_dt(2),
                ),
                CreditLedger(
                    doctor_id=doc1.id,
                    amount=-199.0,
                    reason="invoice_offset",
                    ref_type="invoice",
                    ref_id=inv_prior.id,
                    created_at=_dt(11, year=_YEAR - 1),
                ),
            ]
        )
        s.commit()
    return ids


# --------------------------------------------------------------------------- #
# KPIs
# --------------------------------------------------------------------------- #


def test_kpis_counts(seeded: dict[str, int]) -> None:
    k = ReportingController.kpis()
    assert k.doctors_total == 2
    assert k.doctors_verified == 1
    assert k.patients_total == 1
    assert k.appointments_by_status == {
        str(AppointmentStatus.COMPLETED): 2,
        str(AppointmentStatus.CONFIRMED): 1,
        str(AppointmentStatus.CANCELLED): 1,
    }
    assert k.reviews_published == 2
    assert k.referrals_rewarded == 1
    assert k.active_subscriptions == 2


def test_kpis_empty(db: sessionmaker[Session]) -> None:
    k = ReportingController.kpis()
    assert k.doctors_total == 0
    assert k.patients_total == 0
    assert k.appointments_by_status == {}
    assert k.active_subscriptions == 0


# --------------------------------------------------------------------------- #
# Revenue summary
# --------------------------------------------------------------------------- #


def test_revenue_summary_buckets_and_mrr(seeded: dict[str, int]) -> None:
    r = ReportingController.revenue_summary(_YEAR)
    assert r.year == _YEAR
    assert r.currency == "MAD"
    # Only the two in-year cash payments; the prior-year one is excluded.
    assert r.total_collected == 199.0 + 299.0
    assert [(m.month, m.collected, m.payments) for m in r.by_month] == [
        (2, 199.0, 1),
        (5, 299.0, 1),
    ]
    assert r.active_subscriptions == 2
    # MRR = sum of active plan prices (199 + 299).
    assert r.mrr == 498.0


def test_revenue_summary_other_year_empty(seeded: dict[str, int]) -> None:
    r = ReportingController.revenue_summary(_YEAR - 1)
    # The prior-year payment lands here instead.
    assert r.total_collected == 199.0
    assert [(m.month, m.payments) for m in r.by_month] == [(11, 1)]
    # MRR is a current run-rate, independent of the queried year.
    assert r.mrr == 498.0


# --------------------------------------------------------------------------- #
# Accounting export
# --------------------------------------------------------------------------- #


def test_accounting_export_in_year_only_sorted(seeded: dict[str, int]) -> None:
    rows = ReportingController.accounting_export(_YEAR)

    kinds = [r.kind for r in rows]
    # 2 invoices + 2 payments + 1 credit dated in-year.
    assert kinds.count("INVOICE") == 2
    assert kinds.count("PAYMENT") == 2
    assert kinds.count("CREDIT") == 1
    assert len(rows) == 5

    # Sorted by date ascending.
    dates = [r.date for r in rows]
    assert dates == sorted(dates)

    # Stable column set present on every row.
    for row in rows:
        assert row.kind in {"INVOICE", "PAYMENT", "CREDIT"}
        assert isinstance(row.reference, str)
        assert isinstance(row.doctor_id, int)
        assert isinstance(row.amount, float)
        assert row.currency == "MAD"
        assert isinstance(row.detail, str)

    # Prior-year invoice / payment / credit are excluded.
    refs = {r.reference for r in rows}
    assert "R-PRIOR" not in refs
    assert str(seeded["inv_prior"]) not in {r.reference for r in rows if r.kind == "INVOICE"}

    # Payment row carries the receipt as its reference and the invoice's doctor.
    pay = next(r for r in rows if r.kind == "PAYMENT" and r.reference == "R-FEB")
    assert pay.doctor_id == seeded["doc1"]
    assert pay.detail == "CASH"


def test_accounting_export_prior_year(seeded: dict[str, int]) -> None:
    rows = ReportingController.accounting_export(_YEAR - 1)
    kinds = sorted(r.kind for r in rows)
    # One invoice, one payment, one credit — all in the prior year.
    assert kinds == ["CREDIT", "INVOICE", "PAYMENT"]
    assert {r.reference for r in rows} >= {"R-PRIOR"}
