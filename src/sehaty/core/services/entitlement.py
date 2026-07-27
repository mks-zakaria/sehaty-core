"""What a doctor is entitled to, given the state of their subscription.

The rule this encodes is a commercial decision, not a technical one, and it runs
in exactly one direction:

    An unpaid subscription disables the **booking engine**.
    It never takes the doctor's page down.

The public page, the QR code on the waiting-room wall and the Appeler /
WhatsApp / Itinéraire buttons keep working forever, for everyone. Three reasons:

* It is what the sales sheet promises in print ("Votre page reste en ligne
  gratuitement, à vie"). Silently breaking that is worse than never saying it.
* Free pages are what keep the city listings dense, and a dense listing is what
  makes the marketplace worth anything to patients.
* A printed plaque outlives a subscription. A QR code that leads to a dead page
  is a physical object in a real waiting room telling patients the business
  failed.

So expiry degrades the product to what a doctor had before they ever paid, and
no further.
"""

from datetime import UTC, datetime, timedelta

from sehaty.db import Plan, Subscription, SubscriptionStatus
from sqlalchemy import select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session

# Statuses that still entitle a doctor to the booking engine. TRIALING counts:
# the three free months are sold as the real product, not a crippled preview.
_ENTITLED = {SubscriptionStatus.TRIALING, SubscriptionStatus.ACTIVE}

# Days past `current_period_end` before booking is switched off. A cabinet whose
# cheque cleared on Tuesday should not lose its agenda on Monday, and chasing a
# late payment is a phone call, not an outage.
GRACE_DAYS = 7


class Entitlement(DomainModel):
    """Whether a doctor currently gets the paid features, and why."""

    doctor_id: int
    booking_enabled: bool
    # The raw subscription status, or None when they never subscribed.
    status: str | None
    current_period_end: datetime | None
    in_grace_period: bool
    # Machine-readable cause, for the UI to phrase: "no_subscription",
    # "expired", "cancelled", "past_due", or "active".
    reason: str


def _as_utc(value: datetime | None) -> datetime | None:
    """Postgres returns aware datetimes, SQLite naive; normalize before compare."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def entitlement_for(doctor_id: int, *, now: datetime | None = None) -> Entitlement:
    """Resolve one doctor's current entitlement.

    A doctor with no subscription row at all is *not* an error: most doctors on
    the platform are unclaimed imports who never subscribed. They simply have no
    booking engine.
    """
    now = now or datetime.now(UTC)

    with get_session() as session:
        subscription = session.execute(
            select(Subscription)
            .where(Subscription.doctor_id == doctor_id)
            .order_by(Subscription.current_period_end.desc())
            .limit(1)
        ).scalar_one_or_none()

        if subscription is None:
            return Entitlement(
                doctor_id=doctor_id,
                booking_enabled=False,
                status=None,
                current_period_end=None,
                in_grace_period=False,
                reason="no_subscription",
            )

        status = subscription.status
        period_end = _as_utc(subscription.current_period_end)
        expired = period_end is not None and period_end < now
        grace_end = period_end + timedelta(days=GRACE_DAYS) if period_end else None
        in_grace = bool(expired and grace_end and now <= grace_end)

        if status == SubscriptionStatus.CANCELLED:
            # Cancelling is an explicit choice, so it takes effect at the end of
            # the paid period rather than immediately — they paid for it.
            enabled = not expired
            reason = "cancelled" if expired else "active"
        elif status not in _ENTITLED:
            # PAST_DUE keeps working through the grace window.
            enabled = in_grace
            reason = "past_due"
        elif expired and not in_grace:
            enabled = False
            reason = "expired"
        else:
            enabled = True
            reason = "active"

        return Entitlement(
            doctor_id=doctor_id,
            booking_enabled=enabled,
            status=str(status),
            current_period_end=period_end,
            in_grace_period=in_grace,
            reason=reason if not enabled else "active",
        )


def booking_enabled(doctor_id: int, *, now: datetime | None = None) -> bool:
    """Shorthand for the one question most callers actually have."""
    return entitlement_for(doctor_id, now=now).booking_enabled


# Length of the free trial a newly accredited doctor gets. Matches the three
# months the Pack Présence promises; the sale itself converts this to ACTIVE.
TRIAL_DAYS = 90


def start_trial_if_absent(
    doctor_id: int, *, plan_code: str = "basic", now: datetime | None = None
) -> bool:
    """Give a doctor a TRIALING subscription if they have none. Returns True if created.

    Called when an admin accredits a doctor — the moment they go live. Without
    it the entitlement check would switch off the agenda of every doctor who
    was never explicitly subscribed, which is every newly accredited one.

    Idempotent: a doctor who already has any subscription is left alone, so
    re-accrediting someone never resets or extends their paid period.
    """
    now = now or datetime.now(UTC)

    with get_session() as session:
        existing = session.execute(
            select(Subscription.id).where(Subscription.doctor_id == doctor_id).limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return False

        plan = session.execute(select(Plan).where(Plan.code == plan_code)).scalar_one_or_none()
        if plan is None:
            # Fall back to any active plan rather than failing accreditation:
            # a missing plan catalogue is an ops problem, not a reason to block
            # a doctor from going live.
            plan = session.execute(
                select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.id.asc()).limit(1)
            ).scalar_one_or_none()
        if plan is None:
            return False

        session.add(
            Subscription(
                doctor_id=doctor_id,
                plan_id=plan.id,
                status=SubscriptionStatus.TRIALING,
                current_period_start=now,
                current_period_end=now + timedelta(days=TRIAL_DAYS),
            )
        )
        return True
