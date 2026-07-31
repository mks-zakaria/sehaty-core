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

There is a second, independent reason a cabinet has no agenda: they do not want
one. Walk-ins only, or a secretary away for the month. That is what
``doctor_booking_switches`` holds — a hand switch staff throw at the desk, which
answers a different question from "have they paid" and must not be expressed by
cancelling a subscription they are paying for.

The two compose in one direction only:

    booking = entitled(subscription)  AND NOT manually disabled

So the switch can take booking away and never grant it. An expired subscription
still closes the agenda by itself, and nobody ends up with the paid engine for
free because a flag was left on.
"""

from datetime import UTC, datetime, timedelta

from sehaty.db import DoctorBookingSwitch, Plan, Subscription, SubscriptionStatus, User, UserRole
from sqlalchemy import select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError

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
    # "expired", "cancelled", "past_due", "switched_off", or "active".
    reason: str
    # True when staff switched the agenda off by hand. Reported separately from
    # `reason` because it answers a different question for whoever is looking:
    # `reason` says why booking is off, this says whether *money* is the cause.
    # A collections board that chases a doctor who simply does not want an agenda
    # burns the relationship the packs are sold on.
    manually_disabled: bool = False


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
        switched_off = (
            session.execute(
                select(DoctorBookingSwitch.disabled_at).where(
                    DoctorBookingSwitch.doctor_id == doctor_id
                )
            ).scalar_one_or_none()
            is not None
        )

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
                # Never paid *and* switched off by hand: say the hand switch, so
                # nobody "fixes" it by taking a payment that changes nothing.
                reason="switched_off" if switched_off else "no_subscription",
                manually_disabled=switched_off,
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

        # The hand switch is applied last and only ever subtracts, so a doctor
        # who stops paying still loses the agenda on schedule.
        if switched_off:
            reason = "switched_off"
            enabled = False

        return Entitlement(
            doctor_id=doctor_id,
            booking_enabled=enabled,
            status=str(status),
            current_period_end=period_end,
            in_grace_period=in_grace,
            reason=reason if not enabled else "active",
            manually_disabled=switched_off,
        )


def booking_enabled(doctor_id: int, *, now: datetime | None = None) -> bool:
    """Shorthand for the one question most callers actually have."""
    return entitlement_for(doctor_id, now=now).booking_enabled


def set_booking(
    doctor_id: int,
    *,
    enabled: bool,
    note: str | None = None,
    now: datetime | None = None,
) -> Entitlement:
    """Turn a doctor's booking engine on or off from the console.

    This is the switch thrown at the cabinet while the pack is being sold, so it
    does the whole of what "activate" means there rather than half of it:

    * **on** clears any hand switch *and* starts the free trial if the doctor has
      no subscription at all — which is every doctor who has just been sold their
      first pack. Starting the trial is idempotent, so switching off and on again
      never buys them another ninety days.
    * **off** records the switch. It survives payment, renewal and dunning,
      because a doctor who does not want an agenda has not stopped being a
      customer.

    Returns the entitlement as it now stands, so the caller shows the doctor the
    real state rather than the one it assumes it just created — "on" for a doctor
    whose subscription expired months ago correctly reports booking still off.
    """
    now = now or datetime.now(UTC)

    with get_session() as session:
        doctor = session.get(User, doctor_id)
        if doctor is None or doctor.role != UserRole.DOCTOR:
            raise SehatyNotFoundError(f"no doctor with id {doctor_id}")

        switch = session.get(DoctorBookingSwitch, doctor_id)
        if switch is None:
            switch = DoctorBookingSwitch(doctor_id=doctor_id)
            session.add(switch)
        switch.disabled_at = None if enabled else now
        if note is not None:
            switch.note = note.strip() or None
        session.flush()

    if enabled:
        # After clearing the switch, not before: a doctor who is only blocked by
        # hand keeps the subscription they already have.
        start_trial_if_absent(doctor_id, now=now)

    return entitlement_for(doctor_id, now=now)


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
