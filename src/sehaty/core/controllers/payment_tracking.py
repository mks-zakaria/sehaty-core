"""Who has paid, who has not, and who is about to lose their agenda.

The operator view behind the admin console's payments page. Its job is to answer
one question at a glance — *which cabinets do I need to call this week* — so it
leads with the doctors closest to losing the booking engine rather than with an
alphabetical list of everybody.

Reads only. Recording a payment stays in ``BillingController.record_cash_payment``,
which is idempotent per receipt number.
"""

from datetime import UTC, datetime, timedelta

from sehaty.db import (
    DoctorProfile,
    Invoice,
    InvoiceStatus,
    Plan,
    Subscription,
    User,
)
from sqlalchemy import func, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyValidationError
from sehaty.core.services.entitlement import GRACE_DAYS, entitlement_for

# "Expiring soon" horizon for the collection list.
_SOON_DAYS = 14
_MAX_LIMIT = 500


class PaymentRow(DomainModel):
    """One doctor's billing position."""

    doctor_id: int
    slug: str
    full_name: str
    city: str | None
    phone: str | None
    plan: str | None
    status: str | None
    current_period_end: datetime | None
    # Days until the period ends; negative once it has passed.
    days_remaining: int | None
    amount_due: float
    open_invoices: int
    last_payment_at: datetime | None
    booking_enabled: bool
    in_grace_period: bool
    # What to do about it: "ok", "expiring_soon", "grace", "suspended",
    # "never_subscribed".
    action: str


class PaymentBoard(DomainModel):
    """The collection list plus the counts worth seeing first."""

    rows: list[PaymentRow]
    total_due: float
    suspended: int
    in_grace: int
    expiring_soon: int


def _action(entitlement, days_remaining: int | None) -> str:
    """Bucket a doctor into the thing the operator should actually do."""
    if entitlement.status is None:
        return "never_subscribed"
    if entitlement.in_grace_period:
        return "grace"
    if not entitlement.booking_enabled:
        return "suspended"
    if days_remaining is not None and days_remaining <= _SOON_DAYS:
        return "expiring_soon"
    return "ok"


# Most urgent first: a suspended cabinet is already losing bookings, one in
# grace is days away, one expiring soon is this week's phone call.
_ACTION_ORDER = {
    "suspended": 0,
    "grace": 1,
    "expiring_soon": 2,
    "never_subscribed": 3,
    "ok": 4,
}


class PaymentTrackingController:
    @staticmethod
    def board(*, limit: int = 200, now: datetime | None = None) -> PaymentBoard:
        """Every doctor's billing position, most urgent first."""
        if limit <= 0 or limit > _MAX_LIMIT:
            raise SehatyValidationError(f"limit out of range: {limit}")
        now = now or datetime.now(UTC)

        with get_session() as session:
            doctors = session.execute(
                select(
                    DoctorProfile.user_id,
                    DoctorProfile.slug,
                    DoctorProfile.full_name,
                    DoctorProfile.city,
                    User.phone,
                )
                .join(User, User.id == DoctorProfile.user_id)
                .where(User.is_active.is_(True))
                .order_by(DoctorProfile.full_name.asc())
            ).all()

            doctor_ids = [row.user_id for row in doctors]
            plans = _plan_by_doctor(session, doctor_ids)
            dues = _open_invoices_by_doctor(session, doctor_ids)
            last_paid = _last_payment_by_doctor(session, doctor_ids)

        rows: list[PaymentRow] = []
        for row in doctors:
            entitlement = entitlement_for(row.user_id, now=now)
            period_end = entitlement.current_period_end
            days_remaining = (period_end - now).days if period_end else None
            amount_due, open_count = dues.get(row.user_id, (0.0, 0))

            rows.append(
                PaymentRow(
                    doctor_id=row.user_id,
                    slug=row.slug,
                    full_name=row.full_name,
                    city=row.city,
                    phone=row.phone,
                    plan=plans.get(row.user_id),
                    status=entitlement.status,
                    current_period_end=period_end,
                    days_remaining=days_remaining,
                    amount_due=amount_due,
                    open_invoices=open_count,
                    last_payment_at=last_paid.get(row.user_id),
                    booking_enabled=entitlement.booking_enabled,
                    in_grace_period=entitlement.in_grace_period,
                    action=_action(entitlement, days_remaining),
                )
            )

        rows.sort(
            key=lambda r: (
                _ACTION_ORDER.get(r.action, 9),
                r.days_remaining if r.days_remaining is not None else 9999,
                r.full_name,
            )
        )

        return PaymentBoard(
            rows=rows[:limit],
            total_due=round(sum(r.amount_due for r in rows), 2),
            suspended=sum(1 for r in rows if r.action == "suspended"),
            in_grace=sum(1 for r in rows if r.action == "grace"),
            expiring_soon=sum(1 for r in rows if r.action == "expiring_soon"),
        )

    @staticmethod
    def expiring_within(days: int = GRACE_DAYS, *, now: datetime | None = None) -> list[PaymentRow]:
        """Doctors whose agenda switches off within ``days`` — the call list."""
        if days <= 0 or days > 365:
            raise SehatyValidationError(f"days out of range: {days}")
        now = now or datetime.now(UTC)
        horizon = now + timedelta(days=days)

        board = PaymentTrackingController.board(limit=_MAX_LIMIT, now=now)
        return [
            row
            for row in board.rows
            if row.current_period_end is not None and row.current_period_end <= horizon
        ]


def _plan_by_doctor(session, doctor_ids: list[int]) -> dict[int, str]:  # noqa: ANN001
    if not doctor_ids:
        return {}
    rows = session.execute(
        select(Subscription.doctor_id, Plan.code, Subscription.current_period_end)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(Subscription.doctor_id.in_(doctor_ids))
        .order_by(Subscription.current_period_end.asc())
    ).all()
    # Ascending, so the newest period overwrites — same row entitlement reads.
    return {row.doctor_id: row.code for row in rows}


def _open_invoices_by_doctor(session, doctor_ids: list[int]) -> dict[int, tuple[float, int]]:  # noqa: ANN001
    if not doctor_ids:
        return {}
    rows = session.execute(
        select(
            Invoice.doctor_id,
            func.coalesce(func.sum(Invoice.amount), 0.0).label("due"),
            func.count(Invoice.id).label("n"),
        )
        .where(Invoice.doctor_id.in_(doctor_ids), Invoice.status == InvoiceStatus.OPEN)
        .group_by(Invoice.doctor_id)
    ).all()
    return {row.doctor_id: (float(row.due), int(row.n)) for row in rows}


def _last_payment_by_doctor(session, doctor_ids: list[int]) -> dict[int, datetime]:  # noqa: ANN001
    """Most recent payment per doctor, joined through their invoices."""
    if not doctor_ids:
        return {}
    from sehaty.db import Payment  # noqa: PLC0415

    rows = session.execute(
        select(Invoice.doctor_id, func.max(Payment.paid_at).label("last_paid"))
        .join(Payment, Payment.invoice_id == Invoice.id)
        .where(Invoice.doctor_id.in_(doctor_ids))
        .group_by(Invoice.doctor_id)
    ).all()
    return {row.doctor_id: row.last_paid for row in rows if row.last_paid}
