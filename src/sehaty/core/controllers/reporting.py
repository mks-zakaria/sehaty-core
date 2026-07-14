"""Read-only reporting + year-end accounting for the founder/admin.

Two audiences, one controller:

* An **operational KPI dashboard** (:meth:`ReportingController.kpis`) — the
  live health of the marketplace: doctors, patients, appointments by status,
  published reviews, rewarded referrals, active subscriptions.
* A **year-end accounting trail** (:meth:`ReportingController.revenue_summary`
  and :meth:`ReportingController.accounting_export`) for comptabilité: the cash
  actually collected, month by month, plus an immutable, date-ordered list of
  every invoice / cash payment / credit-ledger movement in a fiscal year, in a
  stable column set that drops straight into a CSV.

Everything here is read-only: pure ``select(...)`` aggregations over
:func:`get_session`, returning small frozen dataclass projections (never ORM
objects, so callers hold detached, serialisable data). Fiscal-year windows use
explicit UTC ``[Jan 1, next Jan 1)`` datetime bounds compared against the
timestamp columns — never ``extract()`` — so the same query runs on Postgres
and on the SQLite test engine alike.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sehaty.db import (
    CreditLedger,
    Invoice,
    Payment,
    Plan,
    Review,
    ReviewStatus,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
    VerificationStatus,
)
from sehaty.db.referral import Referral, ReferralStatus
from sehaty.db.scheduling import Appointment
from sehaty.db.users import DoctorProfile
from sqlalchemy import func, select

from sehaty.core.db.session import get_session

# Sehaty bills in a single currency; cash is the only rail it tracks.
_CURRENCY = "MAD"
_CASH = "CASH"


@dataclass(frozen=True)
class PlatformKpis:
    """Operational marketplace snapshot (detached, count-only projection)."""

    doctors_total: int
    doctors_verified: int
    patients_total: int
    appointments_by_status: dict[str, int]
    reviews_published: int
    referrals_rewarded: int
    active_subscriptions: int


@dataclass(frozen=True)
class MonthRevenue:
    """Cash collected in one calendar month of a fiscal year."""

    month: int
    collected: float
    payments: int


@dataclass(frozen=True)
class RevenueSummary:
    """A fiscal year's cash picture (collected + recurring run-rate)."""

    year: int
    total_collected: float
    currency: str
    by_month: list[MonthRevenue]
    active_subscriptions: int
    mrr: float


@dataclass(frozen=True)
class LedgerRow:
    """One immutable accounting movement, shaped for a CSV column set.

    ``kind`` is one of ``INVOICE`` | ``PAYMENT`` | ``CREDIT``; ``reference`` is
    the human-facing key for that kind (invoice id, cash receipt number, or the
    credit's originating ref id).
    """

    date: datetime
    kind: str
    reference: str
    doctor_id: int
    amount: float
    currency: str
    detail: str


def _year_bounds(year: int) -> tuple[datetime, datetime]:
    """Half-open UTC window ``[Jan 1 year, Jan 1 year+1)`` for the fiscal year."""
    return (
        datetime(year, 1, 1, tzinfo=UTC),
        datetime(year + 1, 1, 1, tzinfo=UTC),
    )


class ReportingController:
    @staticmethod
    def kpis() -> PlatformKpis:
        """Return the current operational KPIs for the admin dashboard.

        Counts are computed independently with ``func.count()`` (grouped for
        the appointment breakdown). ``doctors_total`` / ``patients_total`` come
        from user roles; ``doctors_verified`` from the doctor-profile
        verification status; reviews are counted only when ``PUBLISHED`` and
        referrals only when ``REWARDED``.
        """
        with get_session() as session:
            doctors_total = session.execute(
                select(func.count()).select_from(User).where(User.role == UserRole.DOCTOR)
            ).scalar_one()
            patients_total = session.execute(
                select(func.count()).select_from(User).where(User.role == UserRole.PATIENT)
            ).scalar_one()
            doctors_verified = session.execute(
                select(func.count())
                .select_from(DoctorProfile)
                .where(DoctorProfile.verification_status == VerificationStatus.VERIFIED)
            ).scalar_one()

            appt_rows = session.execute(
                select(Appointment.status, func.count()).group_by(Appointment.status)
            ).all()
            appointments_by_status = {str(status): count for status, count in appt_rows}

            reviews_published = session.execute(
                select(func.count())
                .select_from(Review)
                .where(Review.status == ReviewStatus.PUBLISHED)
            ).scalar_one()
            referrals_rewarded = session.execute(
                select(func.count())
                .select_from(Referral)
                .where(Referral.status == ReferralStatus.REWARDED)
            ).scalar_one()
            active_subscriptions = session.execute(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.status == SubscriptionStatus.ACTIVE)
            ).scalar_one()

            return PlatformKpis(
                doctors_total=int(doctors_total),
                doctors_verified=int(doctors_verified),
                patients_total=int(patients_total),
                appointments_by_status=appointments_by_status,
                reviews_published=int(reviews_published),
                referrals_rewarded=int(referrals_rewarded),
                active_subscriptions=int(active_subscriptions),
            )

    @staticmethod
    def revenue_summary(year: int) -> RevenueSummary:
        """Summarise cash collected in ``year`` plus the recurring run-rate.

        ``total_collected`` and ``by_month`` derive from ``CASH`` payments whose
        ``paid_at`` falls inside the fiscal year; ``by_month`` holds one entry
        per month that saw cash, ordered ascending. ``mrr`` is the sum of the
        plan prices of every ``ACTIVE`` subscription (its monthly run-rate),
        independent of the year window.
        """
        start, end = _year_bounds(year)
        with get_session() as session:
            payments = session.execute(
                select(Payment.amount, Payment.paid_at).where(
                    Payment.method == _CASH,
                    Payment.paid_at >= start,
                    Payment.paid_at < end,
                )
            ).all()

            buckets: dict[int, tuple[float, int]] = {}
            total = 0.0
            for amount, paid_at in payments:
                total += float(amount)
                collected, count = buckets.get(paid_at.month, (0.0, 0))
                buckets[paid_at.month] = (collected + float(amount), count + 1)

            by_month = [
                MonthRevenue(month=m, collected=buckets[m][0], payments=buckets[m][1])
                for m in sorted(buckets)
            ]

            active_subscriptions = session.execute(
                select(func.count())
                .select_from(Subscription)
                .where(Subscription.status == SubscriptionStatus.ACTIVE)
            ).scalar_one()
            mrr = session.execute(
                select(func.coalesce(func.sum(Plan.price_month), 0.0))
                .select_from(Subscription)
                .join(Plan, Plan.id == Subscription.plan_id)
                .where(Subscription.status == SubscriptionStatus.ACTIVE)
            ).scalar_one()

            return RevenueSummary(
                year=year,
                total_collected=total,
                currency=_CURRENCY,
                by_month=by_month,
                active_subscriptions=int(active_subscriptions),
                mrr=float(mrr),
            )

    @staticmethod
    def accounting_export(year: int) -> list[LedgerRow]:
        """Return the immutable, date-ordered accounting trail for ``year``.

        One :class:`LedgerRow` per movement dated inside the fiscal year:

        * every ``Invoice`` issued (``issued_at``), ``kind=INVOICE``,
          ``reference`` = invoice id, amount = the invoice amount;
        * every ``Payment`` recorded (``paid_at``), ``kind=PAYMENT``,
          ``reference`` = the paper receipt (``provider_ref``), amount = paid;
        * every ``CreditLedger`` row (``created_at``), ``kind=CREDIT``,
          ``reference`` = originating ``ref_id``, amount = the ledger delta.

        Rows are sorted by ``date`` ascending — the stable comptabilité trail.
        """
        start, end = _year_bounds(year)
        rows: list[LedgerRow] = []
        with get_session() as session:
            invoices = session.execute(
                select(
                    Invoice.id,
                    Invoice.doctor_id,
                    Invoice.amount,
                    Invoice.currency,
                    Invoice.status,
                    Invoice.issued_at,
                ).where(Invoice.issued_at >= start, Invoice.issued_at < end)
            ).all()
            for inv_id, doctor_id, amount, currency, status, issued_at in invoices:
                rows.append(
                    LedgerRow(
                        date=issued_at,
                        kind="INVOICE",
                        reference=str(inv_id),
                        doctor_id=int(doctor_id),
                        amount=float(amount),
                        currency=currency,
                        detail=str(status),
                    )
                )

            # Payments carry no doctor/currency of their own; join the invoice.
            payments = session.execute(
                select(
                    Payment.id,
                    Payment.provider_ref,
                    Payment.amount,
                    Payment.method,
                    Payment.paid_at,
                    Invoice.doctor_id,
                    Invoice.currency,
                )
                .join(Invoice, Invoice.id == Payment.invoice_id)
                .where(Payment.paid_at >= start, Payment.paid_at < end)
            ).all()
            for pay_id, provider_ref, amount, method, paid_at, doctor_id, currency in payments:
                rows.append(
                    LedgerRow(
                        date=paid_at,
                        kind="PAYMENT",
                        reference=provider_ref or str(pay_id),
                        doctor_id=int(doctor_id),
                        amount=float(amount),
                        currency=currency,
                        detail=method,
                    )
                )

            credits = session.execute(
                select(
                    CreditLedger.id,
                    CreditLedger.doctor_id,
                    CreditLedger.amount,
                    CreditLedger.reason,
                    CreditLedger.ref_id,
                    CreditLedger.created_at,
                ).where(CreditLedger.created_at >= start, CreditLedger.created_at < end)
            ).all()
            for cl_id, doctor_id, amount, reason, ref_id, created_at in credits:
                rows.append(
                    LedgerRow(
                        date=created_at,
                        kind="CREDIT",
                        reference=str(ref_id) if ref_id is not None else str(cl_id),
                        doctor_id=int(doctor_id),
                        amount=float(amount),
                        currency=_CURRENCY,
                        detail=reason,
                    )
                )

        rows.sort(key=lambda r: r.date)
        return rows
