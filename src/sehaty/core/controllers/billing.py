"""Cash-billing business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). Doctors
subscribe to a monthly plan; the platform issues an ``OPEN`` invoice per period
and an admin records the *cash* the doctor hands over at the desk. Sehaty never
touches an online payment rail — it only **tracks** cash: every receipt becomes
a ``Payment(method="CASH")`` keyed by its paper receipt number, which makes
recording idempotent (re-entering the same receipt is a no-op, never a double
charge). Referral rewards land in the append-only ``CreditLedger`` and offset
the next invoice. Overdue invoices flip their subscription ``PAST_DUE`` via the
dunning sweep.

Failures raise the ``SehatyError`` taxonomy; methods never return ``None`` to
signal an error. All IO flows through ``get_session``.
"""

from datetime import UTC, datetime, timedelta

from pydantic import Field
from sehaty.db import (
    AuditLog,
    CreditLedger,
    Invoice,
    InvoiceStatus,
    Payment,
    Plan,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
)
from sqlalchemy import func, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyForbiddenError,
    SehatyNotFoundError,
)
from sehaty.core.services.entitlement import entitlement_for

# The three MAD/month plans Sehaty sells (code, name, price_month).
_SEED_PLANS: tuple[tuple[str, str, float], ...] = (
    ("essentiel", "Essentiel", 199.0),
    ("pro", "Pro", 299.0),
    ("premium", "Premium", 499.0),
)
_CURRENCY = "MAD"
# One billing period. 30 days keeps the arithmetic dialect-agnostic (no month
# math), which is all the cash flow needs.
_PERIOD = timedelta(days=30)


def _billing_state(entitlement, days: int | None) -> str:  # noqa: ANN001
    """One word the interface can act on.

    Distinguishes grace from expired because they need opposite messages: one is
    "your agenda is still running, pay this week", the other is "it has stopped,
    here is how to restart it". Collapsing them would tell half the doctors the
    wrong thing.
    """
    if entitlement.status is None:
        return "never_subscribed"
    if entitlement.in_grace_period:
        return "grace"
    if not entitlement.booking_enabled:
        return "expired"
    if days is not None and days <= 7:
        return "expiring_soon"
    return "active"


class SubscriptionSummary(DomainModel):
    """A doctor's current billing picture (detached, column-only projection).

    Carries the entitlement, not just the invoice, because the question a doctor
    actually has is "does my agenda still work" — and until this existed the app
    could not answer it. A lapsed doctor simply saw an empty agenda, which reads
    as a broken product rather than an unpaid bill.
    """

    plan: str | None
    status: str | None
    amount_due: float
    current_period_end: datetime | None

    # Whether the booking engine is live right now.
    booking_enabled: bool = False
    # True during the days after expiry when it still works.
    in_grace_period: bool = False
    # Negative once the period has passed. None when there is no subscription.
    days_remaining: int | None = None
    # "active", "expiring_soon", "grace", "expired", "never_subscribed" — the
    # thing the page needs in order to say something true.
    state: str = "never_subscribed"


class PlanRow(DomainModel):
    """A catalogue plan as returned by the controller (detached projection).

    The transport layer serialises this directly. ``is_active`` is carried for
    internal callers (e.g. tests) but never serialised — the plan list only ever
    exposes active plans.
    """

    code: str
    name: str
    price_month: float
    currency: str
    is_active: bool = Field(default=None, exclude=True)


class SubscriptionRow(DomainModel):
    """A doctor's subscription row as returned by the controller (detached).

    ``current_period_start`` is carried for internal callers but never
    serialised — the wire contract exposes only the period *end*.
    """

    id: int
    plan_id: int
    status: SubscriptionStatus
    current_period_end: datetime
    current_period_start: datetime = Field(default=None, exclude=True)


class PaymentRow(DomainModel):
    """A recorded cash payment as returned by the controller (detached).

    ``provider_ref`` (the paper receipt number) is carried for internal callers
    but never serialised — the wire contract does not expose the receipt number.
    """

    id: int
    invoice_id: int
    amount: float
    method: str
    paid_at: datetime
    provider_ref: str = Field(default=None, exclude=True)


class BillingController:
    @staticmethod
    def seed_plans() -> list[Plan]:
        """Insert the three catalogue plans; idempotent on plan ``code``.

        Existing codes are skipped, so a second call adds nothing. Returns the
        plans created *by this call* (empty on a re-run).
        """
        created: list[Plan] = []
        with get_session() as session:
            existing = set(session.execute(select(Plan.code)).scalars().all())
            for code, name, price in _SEED_PLANS:
                if code in existing:
                    continue
                plan = Plan(
                    code=code,
                    name=name,
                    price_month=price,
                    currency=_CURRENCY,
                    is_active=True,
                )
                session.add(plan)
                created.append(plan)
            session.flush()
            return created

    @staticmethod
    def list_plans() -> list[PlanRow]:
        """Return every active plan, ordered by monthly price."""
        stmt = select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.price_month)
        with get_session() as session:
            return [PlanRow.model_validate(p) for p in session.execute(stmt).scalars().all()]

    @staticmethod
    def subscribe(doctor_id: int, plan_code: str) -> SubscriptionRow:
        """Start (or switch) a doctor's subscription to ``plan_code``.

        Sets the subscription ``ACTIVE`` with a one-month current period from
        now. A doctor has a single subscription row: an existing one is
        re-pointed at the new plan rather than duplicated. Unknown plan codes
        raise ``SehatyNotFoundError``.
        """
        now = datetime.now(UTC)
        with get_session() as session:
            plan = session.execute(select(Plan).where(Plan.code == plan_code)).scalar_one_or_none()
            if plan is None:
                raise SehatyNotFoundError(f"no plan with code {plan_code!r}")

            sub = session.execute(
                select(Subscription).where(Subscription.doctor_id == doctor_id)
            ).scalar_one_or_none()
            if sub is None:
                sub = Subscription(doctor_id=doctor_id, plan_id=plan.id)
                session.add(sub)
            else:
                sub.plan_id = plan.id
            sub.status = SubscriptionStatus.ACTIVE
            sub.current_period_start = now
            sub.current_period_end = now + _PERIOD
            session.flush()
            return SubscriptionRow.model_validate(sub)

    @staticmethod
    def generate_invoice(doctor_id: int) -> Invoice:
        """Issue an ``OPEN`` invoice for the doctor's current period.

        The amount is the plan's monthly price less any available credit,
        floored at zero. Whatever credit is applied is *consumed* with an
        offsetting (negative) ledger row referencing the invoice, so a reward
        discounts exactly one invoice. The doctor must have a subscription
        (else ``SehatyNotFoundError``). Returns the invoice.
        """
        with get_session() as session:
            sub = session.execute(
                select(Subscription).where(Subscription.doctor_id == doctor_id)
            ).scalar_one_or_none()
            if sub is None:
                raise SehatyNotFoundError(f"doctor {doctor_id} has no subscription")
            plan = session.get(Plan, sub.plan_id)
            if plan is None:  # pragma: no cover - FK guarantees a plan
                raise SehatyNotFoundError(f"subscription {sub.id} has no plan")

            credit = BillingController._credit_balance(session, doctor_id)
            applied = min(credit, plan.price_month) if credit > 0 else 0.0
            amount = max(plan.price_month - applied, 0.0)

            invoice = Invoice(
                doctor_id=doctor_id,
                subscription_id=sub.id,
                amount=amount,
                currency=plan.currency,
                status=InvoiceStatus.OPEN,
                issued_at=sub.current_period_start,
                due_at=sub.current_period_end,
            )
            session.add(invoice)
            session.flush()

            if applied > 0:
                session.add(
                    CreditLedger(
                        doctor_id=doctor_id,
                        amount=-applied,
                        reason="invoice_offset",
                        ref_type="invoice",
                        ref_id=invoice.id,
                    )
                )
                session.flush()
            return invoice

    @staticmethod
    def record_cash_payment(
        admin_id: int,
        invoice_id: int,
        amount: float,
        receipt_no: str,
        paid_at: datetime,
    ) -> PaymentRow:
        """Record cash handed over at the desk against an invoice (ADMIN only).

        Idempotent per ``receipt_no``: the paper receipt number is stored as the
        ``Payment.provider_ref``, so replaying the same receipt returns the
        existing payment untouched — never a second charge. On the first sight
        of a receipt it creates a ``CASH`` payment, marks the invoice ``PAID``
        (stamping ``paid_at``), and writes a ``PAYMENT_CASH`` audit entry. A
        non-admin actor is ``SehatyForbiddenError``; a missing invoice is
        ``SehatyNotFoundError``.

        When this receipt is the doctor's *first* paid invoice, a pending
        referral (if any) is settled — crediting the referrer. That settlement
        is itself idempotent, and replaying a receipt short-circuits above, so a
        referrer is never double-rewarded.
        """
        is_first_paid = False
        doctor_id: int | None = None
        with get_session() as session:
            actor_role = session.execute(
                select(User.role).where(User.id == admin_id)
            ).scalar_one_or_none()
            if actor_role != UserRole.ADMIN:
                raise SehatyForbiddenError("only an admin may record cash payments")

            existing = session.execute(
                select(Payment).where(Payment.provider_ref == receipt_no)
            ).scalar_one_or_none()
            if existing is not None:
                return PaymentRow.model_validate(existing)

            invoice = session.get(Invoice, invoice_id)
            if invoice is None:
                raise SehatyNotFoundError(f"no invoice {invoice_id}")

            payment = Payment(
                invoice_id=invoice_id,
                amount=amount,
                method="CASH",
                provider_ref=receipt_no,
                paid_at=paid_at,
            )
            session.add(payment)
            invoice.status = InvoiceStatus.PAID
            invoice.paid_at = paid_at
            doctor_id = invoice.doctor_id
            session.flush()
            session.add(
                AuditLog(
                    actor_user_id=admin_id,
                    action="PAYMENT_CASH",
                    entity="invoice",
                    entity_id=invoice_id,
                )
            )
            session.flush()
            # First paid invoice for this doctor? (== 1 counting the one above.)
            paid_count = session.execute(
                select(func.count())
                .select_from(Invoice)
                .where(
                    Invoice.doctor_id == doctor_id,
                    Invoice.status == InvoiceStatus.PAID,
                )
            ).scalar_one()
            is_first_paid = paid_count == 1

        # Notify the doctor their invoice was paid AFTER the payment commits (its
        # own session, never nested). A replayed receipt short-circuits above
        # (returns the existing payment) so it never reaches here — no duplicate
        # notification. A notification failure must NEVER break the payment, which
        # is already persisted above.
        if doctor_id is not None:
            try:
                from sehaty.core.controllers.notifications import NotificationController

                NotificationController.notify(
                    doctor_id,
                    kind="payment_recorded",
                    message="A cash payment was recorded for your invoice",
                    entity="invoice",
                    entity_id=invoice_id,
                )
            except Exception:
                pass

        # Settle outside the payment transaction to avoid nesting sessions; the
        # settle is idempotent (only a PENDING referral is ever rewarded).
        if is_first_paid and doctor_id is not None:
            from sehaty.core.controllers.referral import ReferralController

            ReferralController.settle_first_payment(doctor_id)
        return PaymentRow.model_validate(payment)

    @staticmethod
    def apply_credit(
        doctor_id: int,
        amount: float,
        reason: str,
        ref_type: str | None = None,
        ref_id: int | None = None,
    ) -> CreditLedger:
        """Append a credit-ledger row (used by referral rewards later).

        The ledger is append-only; positive ``amount`` is credit, negative is a
        debit. Returns the new row.
        """
        with get_session() as session:
            entry = CreditLedger(
                doctor_id=doctor_id,
                amount=amount,
                reason=reason,
                ref_type=ref_type,
                ref_id=ref_id,
            )
            session.add(entry)
            session.flush()
            return entry

    @staticmethod
    def credit_balance(doctor_id: int) -> float:
        """Return the doctor's net credit balance (sum of the ledger)."""
        with get_session() as session:
            return BillingController._credit_balance(session, doctor_id)

    @staticmethod
    def run_dunning() -> int:
        """Flip subscriptions ``PAST_DUE`` for invoices past due and still OPEN.

        Sweeps every ``OPEN`` invoice whose ``due_at`` is in the past and marks
        the owning subscription ``PAST_DUE``. Returns the number of distinct
        subscriptions flipped.
        """
        now = datetime.now(UTC)
        flipped: set[int] = set()
        with get_session() as session:
            overdue = (
                session.execute(
                    select(Invoice).where(
                        Invoice.status == InvoiceStatus.OPEN,
                        Invoice.due_at < now,
                        Invoice.subscription_id.is_not(None),
                    )
                )
                .scalars()
                .all()
            )
            for invoice in overdue:
                if invoice.subscription_id in flipped:
                    continue
                sub = session.get(Subscription, invoice.subscription_id)
                if sub is None:  # pragma: no cover - FK guarantees a subscription
                    continue
                sub.status = SubscriptionStatus.PAST_DUE
                flipped.add(invoice.subscription_id)
            session.flush()
        return len(flipped)

    @staticmethod
    def subscription_status(doctor_id: int) -> SubscriptionSummary:
        """Summarise a doctor's plan, status, amount due, and period end.

        ``amount_due`` is the total of the doctor's still-``OPEN`` invoices. A
        doctor with no subscription raises ``SehatyNotFoundError``.
        """
        with get_session() as session:
            row = session.execute(
                select(Subscription.status, Subscription.current_period_end, Plan.code)
                .join(Plan, Plan.id == Subscription.plan_id)
                .where(Subscription.doctor_id == doctor_id)
            ).one_or_none()
            if row is None:
                # Not an error. Most doctors never subscribed, and the app needs
                # to render that as a state rather than as a failed request.
                return SubscriptionSummary(
                    plan=None,
                    status=None,
                    amount_due=0.0,
                    current_period_end=None,
                    booking_enabled=False,
                    state="never_subscribed",
                )

            amount_due = session.execute(
                select(func.coalesce(func.sum(Invoice.amount), 0.0)).where(
                    Invoice.doctor_id == doctor_id,
                    Invoice.status == InvoiceStatus.OPEN,
                )
            ).scalar_one()
            entitlement = entitlement_for(doctor_id)
            days = None
            if entitlement.current_period_end is not None:
                days = (entitlement.current_period_end - datetime.now(UTC)).days
            return SubscriptionSummary(
                plan=row.code,
                status=str(row.status),
                amount_due=float(amount_due),
                current_period_end=row.current_period_end,
                booking_enabled=entitlement.booking_enabled,
                in_grace_period=entitlement.in_grace_period,
                days_remaining=days,
                state=_billing_state(entitlement, days),
            )

    @staticmethod
    def _credit_balance(session, doctor_id: int) -> float:
        """Net ledger balance within an open session."""
        total = session.execute(
            select(func.coalesce(func.sum(CreditLedger.amount), 0.0)).where(
                CreditLedger.doctor_id == doctor_id
            )
        ).scalar_one()
        return float(total)
