"""The field list: every doctor on the platform, and where they stand.

This is the screen the founder works from, not one a doctor ever sees. The
payment board next door answers "who owes money"; it can only see doctors who
have a subscription at all, so every imported page — which is most of them, and
all of the sales pipeline — is invisible to it.

Two facts drive the whole visit:

* **Onboarded or not.** An imported page is live and unclaimed: the doctor does
  not know it exists yet. That is the cold-call list. A CLAIMED or VERIFIED
  page means the conversation already happened.
* **What they are on.** Either the free landing page, or the landing page plus
  the booking engine they pay monthly for. The page is free forever, so "not
  paying" is a normal steady state and not a failure — the tier says which
  conversation to have, not whether something is wrong.

Address is carried through because the point of the list is driving to it. No
geocoding runs here: imported rows have no coordinates, and a maps deep link
built from the written address works fine without them.
"""

from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sehaty.db import (
    ClaimStatus,
    DoctorLanding,
    DoctorProfile,
    DoctorSpecialty,
    GeoPrecision,
    Specialty,
    Subscription,
    SubscriptionStatus,
)
from sqlalchemy import cast, func, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.services.entitlement import entitlement_for

# What the doctor is actually on today.
PLAN_LANDING = "landing"
PLAN_LANDING_RDV = "landing_rdv"


class ProspectRow(DomainModel):
    """One doctor as a sales prospect."""

    doctor_id: int
    slug: str
    full_name: str
    specialty: str | None
    city: str | None
    district: str | None
    address: str | None
    phone_fixe: str | None
    phone_mobile: str | None
    whatsapp: str | None
    lat: float | None
    lng: float | None

    # UNCLAIMED / CLAIMED / VERIFIED / REMOVAL_REQUESTED
    claim_status: str
    # True once the doctor has been spoken to and taken the page over.
    onboarded: bool
    # IMPORT (we put them on the map) vs SELF_SIGNUP (they came to us).
    source: str | None

    # "landing" — the free page. "landing_rdv" — page plus booking engine.
    plan: str
    booking_enabled: bool
    subscription_status: str | None
    # The 500 MAD Pack Présence: their own services, photos and copy.
    is_personalized: bool

    # What to type into Maps. Prefers coordinates, falls back to the written
    # address — imported rows have no coordinates and still need to be driven
    # to.
    maps_query: str
    # True while the pin is a geocoded quartier centroid or missing entirely.
    # The visit is the chance to fix it, so the list has to say so before the
    # visit rather than after.
    needs_pin: bool


class ProspectBoard(DomainModel):
    """The rows to work, plus the counters that say where the business stands.

    The counters describe the whole city/district scope — not the filtered page.
    Filtering to "not onboarded yet" is how the list is *worked*; if the header
    followed the filter it would read "0 onboarded" at precisely the moment the
    operator is looking at everyone still to visit.
    """

    rows: list[ProspectRow]
    # Rows on this page: after the onboarded/plan filters and the row limit.
    shown: int
    # Doctors in scope, before those filters and before the limit.
    total: int
    onboarded: int
    # Money actually arriving: a live ACTIVE subscription. A trial is not a sale
    # and must never be counted as one — the whole point of the number is to say
    # how far off 10 000 MAD/month we are.
    paying: int
    # On the free 90-day trial. Flipping a doctor's RDV switch on at the desk
    # starts one, so this is the follow-up list: they are using the agenda and
    # have not been asked for money yet.
    trialing: int
    # Bought the Pack Présence. Independent of the agenda: it is a one-off, so a
    # doctor can be presence-paid and still show up as not paying monthly.
    presence: int
    # Districts present in the result, for the filter chips.
    districts: list[str]


def _lapsed(period_end: datetime | None, now: datetime) -> bool:
    """True once the paid period has run out.

    An ACTIVE row whose period ended and was never renewed is not revenue; it is
    someone who stopped paying and whose status nobody has moved yet.
    """
    if period_end is None:
        return False
    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=UTC)
    return period_end < now


def _maps_query(row) -> str:  # noqa: ANN001
    """Coordinates when we have them, otherwise the address plus the city."""
    if row.lat is not None and row.lng is not None:
        return f"{row.lat},{row.lng}"
    parts = [p for p in (row.address, row.district, row.city) if p]
    return ", ".join(parts) or row.full_name


class ProspectController:
    @staticmethod
    def board(
        *,
        city: str | None = None,
        district: str | None = None,
        onboarded: bool | None = None,
        plan: str | None = None,
        limit: int = 500,
        now: datetime | None = None,
    ) -> ProspectBoard:
        """Every doctor, newest import last, with their standing.

        Ordered by district then name so the list reads as a route: everyone in
        Errahma together, alphabetical, which is how you actually walk a
        quartier.
        """
        now = now or datetime.now(UTC)

        # One specialty per doctor — the first by name, matching how the landing
        # template is resolved, so the two screens never disagree.
        primary_specialty = (
            select(
                DoctorSpecialty.doctor_id.label("doctor_id"),
                func.min(Specialty.name_fr).label("name_fr"),
            )
            .join(Specialty, Specialty.id == DoctorSpecialty.specialty_id)
            .group_by(DoctorSpecialty.doctor_id)
            .subquery()
        )

        stmt = (
            select(
                DoctorProfile.user_id,
                DoctorProfile.slug,
                DoctorProfile.full_name,
                DoctorProfile.city,
                DoctorProfile.district,
                DoctorProfile.address,
                DoctorProfile.phone_fixe,
                DoctorProfile.phone_mobile,
                DoctorProfile.whatsapp,
                DoctorProfile.claim_status,
                DoctorProfile.source,
                DoctorProfile.geo_precision,
                # The column is geography; ST_X/ST_Y need a geometry cast.
                func.ST_Y(cast(DoctorProfile.geopoint, Geometry)).label("lat"),
                func.ST_X(cast(DoctorProfile.geopoint, Geometry)).label("lng"),
                primary_specialty.c.name_fr.label("specialty"),
                DoctorLanding.is_personalized,
            )
            .outerjoin(primary_specialty, primary_specialty.c.doctor_id == DoctorProfile.user_id)
            .outerjoin(DoctorLanding, DoctorLanding.doctor_id == DoctorProfile.user_id)
            .where(DoctorProfile.claim_status != ClaimStatus.REMOVAL_REQUESTED)
            .order_by(DoctorProfile.district.asc().nullslast(), DoctorProfile.full_name.asc())
            .limit(limit)
        )
        if city:
            stmt = stmt.where(DoctorProfile.city == city)
        if district:
            stmt = stmt.where(DoctorProfile.district == district)

        # The counters answer "where does the business stand", so they are taken
        # over the whole city/district scope rather than over the page of rows:
        # unaffected by the onboarded/plan filters, and never truncated by the
        # limit the way a count of returned rows would be.
        scope = select(DoctorProfile.user_id).where(
            DoctorProfile.claim_status != ClaimStatus.REMOVAL_REQUESTED
        )
        if city:
            scope = scope.where(DoctorProfile.city == city)
        if district:
            scope = scope.where(DoctorProfile.district == district)

        directory = (
            select(
                func.count(),
                func.count().filter(
                    DoctorProfile.claim_status.in_([ClaimStatus.CLAIMED, ClaimStatus.VERIFIED])
                ),
                func.count().filter(DoctorLanding.is_personalized.is_(True)),
            )
            .select_from(DoctorProfile)
            .outerjoin(DoctorLanding, DoctorLanding.doctor_id == DoctorProfile.user_id)
            .where(DoctorProfile.user_id.in_(scope))
        )

        # One row per doctor, their most recent subscription — the same one
        # `entitlement_for` resolves, so the header and the rows cannot disagree.
        latest_subscription = (
            select(
                Subscription.doctor_id,
                Subscription.status,
                Subscription.current_period_end,
            )
            .where(Subscription.doctor_id.in_(scope))
            .order_by(Subscription.doctor_id, Subscription.current_period_end.desc())
            .distinct(Subscription.doctor_id)
        )

        with get_session() as session:
            records = session.execute(stmt).all()
            total, onboarded_count, presence_count = session.execute(directory).one()
            subscriptions = session.execute(latest_subscription).all()

        paying = trialing = 0
        for subscription in subscriptions:
            if _lapsed(subscription.current_period_end, now):
                continue
            if subscription.status == SubscriptionStatus.ACTIVE:
                paying += 1
            elif subscription.status == SubscriptionStatus.TRIALING:
                trialing += 1

        rows: list[ProspectRow] = []
        for record in records:
            entitlement = entitlement_for(record.user_id, now=now)
            is_onboarded = str(record.claim_status) in {
                str(ClaimStatus.CLAIMED),
                str(ClaimStatus.VERIFIED),
            }
            row_plan = PLAN_LANDING_RDV if entitlement.booking_enabled else PLAN_LANDING

            if onboarded is not None and is_onboarded != onboarded:
                continue
            if plan and row_plan != plan:
                continue

            rows.append(
                ProspectRow(
                    doctor_id=record.user_id,
                    slug=record.slug,
                    full_name=record.full_name,
                    specialty=record.specialty,
                    city=record.city,
                    district=record.district,
                    address=record.address,
                    phone_fixe=record.phone_fixe,
                    phone_mobile=record.phone_mobile,
                    whatsapp=record.whatsapp,
                    lat=record.lat,
                    lng=record.lng,
                    claim_status=str(record.claim_status),
                    onboarded=is_onboarded,
                    source=str(record.source) if record.source else None,
                    plan=row_plan,
                    booking_enabled=entitlement.booking_enabled,
                    subscription_status=entitlement.status,
                    is_personalized=bool(record.is_personalized),
                    maps_query=_maps_query(record),
                    needs_pin=record.geo_precision != GeoPrecision.EXACT,
                )
            )

        return ProspectBoard(
            rows=rows,
            shown=len(rows),
            total=total,
            onboarded=onboarded_count,
            paying=paying,
            trialing=trialing,
            presence=presence_count,
            districts=sorted({r.district for r in rows if r.district}),
        )
