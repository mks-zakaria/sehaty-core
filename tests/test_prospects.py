"""The field list.

Runs against real PostGIS: the board reads ``ST_X``/``ST_Y`` off the geometry
column, which stock SQLite cannot compile.

The case that matters most is the imported doctor — unclaimed, no subscription,
no coordinates. That row is the entire sales pipeline, and it is exactly the
shape the billing board cannot see.
"""

from datetime import UTC, datetime, timedelta

import pytest
from geoalchemy2.elements import WKTElement
from sehaty.db import (
    ClaimStatus,
    DoctorLanding,
    DoctorProfile,
    GeoPrecision,
    Plan,
    ProfileSource,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from sehaty.core.controllers.prospects import (
    PLAN_LANDING,
    PLAN_LANDING_RDV,
    ProspectController,
)

NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
_LAT, _LNG, _SRID = 33.5731104, -7.5898434, 4326


def _doctor(
    session: Session,
    *,
    email: str,
    slug: str,
    district: str,
    address: str | None = None,
    claim: ClaimStatus = ClaimStatus.UNCLAIMED,
    source: ProfileSource = ProfileSource.IMPORT,
    geo: bool = False,
    subscribed: bool = False,
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
    period_end: datetime | None = None,
) -> int:
    user = User(email=email, role=UserRole.DOCTOR, is_active=True)
    session.add(user)
    session.commit()
    session.add(
        DoctorProfile(
            user_id=user.id,
            full_name=slug.replace("-", " ").title(),
            slug=slug,
            license_no=f"LIC-{user.id}",
            city="Casablanca",
            district=district,
            address=address,
            claim_status=claim,
            source=source,
            geopoint=(WKTElement(f"POINT({_LNG} {_LAT})", srid=_SRID) if geo else None),
            geo_precision=GeoPrecision.EXACT if geo else None,
        )
    )
    session.commit()
    if subscribed:
        plan = session.execute(select(Plan).limit(1)).scalar_one_or_none()
        if plan is None:
            plan = Plan(code="p", name="Pack", price_month=199, is_active=True)
            session.add(plan)
            session.commit()
        session.add(
            Subscription(
                doctor_id=user.id,
                plan_id=plan.id,
                status=status,
                current_period_start=NOW - timedelta(days=1),
                current_period_end=period_end or NOW + timedelta(days=30),
            )
        )
        session.commit()
    return user.id


@pytest.mark.usefixtures("_pg_engine")
class TestProspectBoard:
    def test_an_imported_doctor_appears_as_an_unworked_prospect(self, pg_session: Session) -> None:
        """The row the billing board cannot show: no subscription, no coords."""
        _doctor(
            pg_session,
            email="imp@c.ma",
            slug="dr-imported",
            district="Errahma",
            address="Madinat Errahma, bloc U4, n°107",
        )

        board = ProspectController.board(now=NOW)

        (row,) = board.rows
        assert row.onboarded is False
        assert row.claim_status == str(ClaimStatus.UNCLAIMED)
        assert row.plan == PLAN_LANDING
        assert row.source == str(ProfileSource.IMPORT)
        assert board.onboarded == 0
        assert board.paying == 0

    def test_maps_query_falls_back_to_the_written_address(self, pg_session: Session) -> None:
        """Imported rows have no coordinates and still have to be driven to."""
        _doctor(
            pg_session,
            email="noco@c.ma",
            slug="dr-nocoords",
            district="Errahma",
            address="Madinat Errahma, bloc U2, n°21",
        )

        (row,) = ProspectController.board(now=NOW).rows

        assert row.lat is None
        assert row.maps_query == "Madinat Errahma, bloc U2, n°21, Errahma, Casablanca"

    def test_maps_query_prefers_coordinates_when_present(self, pg_session: Session) -> None:
        _doctor(
            pg_session,
            email="geo@c.ma",
            slug="dr-geo",
            district="Maârif",
            address="12 rue X",
            geo=True,
        )

        (row,) = ProspectController.board(now=NOW).rows

        assert row.maps_query == f"{_LAT},{_LNG}"

    def test_a_paying_doctor_is_on_the_rdv_plan(self, pg_session: Session) -> None:
        _doctor(
            pg_session,
            email="pay@c.ma",
            slug="dr-paying",
            district="Maârif",
            claim=ClaimStatus.VERIFIED,
            source=ProfileSource.SELF_SIGNUP,
            subscribed=True,
        )

        board = ProspectController.board(now=NOW)

        (row,) = board.rows
        assert row.plan == PLAN_LANDING_RDV
        assert row.onboarded is True
        assert board.paying == 1
        assert board.trialing == 0

    def test_a_doctor_on_the_free_trial_is_not_counted_as_paying(self, pg_session: Session) -> None:
        """Flipping the RDV switch on at the desk opens a 90-day trial.

        The agenda works, so the row is on the RDV plan — but nobody has paid,
        and a header that says otherwise reports revenue that does not exist.
        """
        _doctor(
            pg_session,
            email="trial@c.ma",
            slug="dr-trial",
            district="Maârif",
            claim=ClaimStatus.CLAIMED,
            subscribed=True,
            status=SubscriptionStatus.TRIALING,
        )

        board = ProspectController.board(now=NOW)

        (row,) = board.rows
        assert row.plan == PLAN_LANDING_RDV
        assert row.booking_enabled is True
        assert board.paying == 0
        assert board.trialing == 1

    def test_a_lapsed_subscription_stops_counting_as_revenue(self, pg_session: Session) -> None:
        """Still ACTIVE in the table, but the paid period ran out in June."""
        _doctor(
            pg_session,
            email="lapsed@c.ma",
            slug="dr-lapsed",
            district="Maârif",
            claim=ClaimStatus.CLAIMED,
            subscribed=True,
            period_end=NOW - timedelta(days=45),
        )

        board = ProspectController.board(now=NOW)

        assert board.paying == 0
        assert board.trialing == 0

    def test_the_counters_describe_the_directory_not_the_filtered_page(
        self, pg_session: Session
    ) -> None:
        """Filtering to the doctors still to visit must not zero the header.

        That filter is how the list is worked, so it is exactly when the founder
        is looking at the counters.
        """
        _doctor(pg_session, email="cold@c.ma", slug="dr-cold", district="Errahma")
        _doctor(
            pg_session,
            email="won@c.ma",
            slug="dr-won",
            district="Maârif",
            claim=ClaimStatus.VERIFIED,
            subscribed=True,
        )

        board = ProspectController.board(onboarded=False, now=NOW)

        assert board.shown == 1
        assert [r.slug for r in board.rows] == ["dr-cold"]
        assert board.total == 2
        assert board.onboarded == 1
        assert board.paying == 1

    def test_the_presence_pack_is_counted_even_without_an_agenda(self, pg_session: Session) -> None:
        """Pack Présence is a one-off sale; it has nothing to do with booking.

        Counted separately or the board shows a doctor who paid 600 MAD sitting
        in the same bucket as one who has paid nothing.
        """
        doctor_id = _doctor(
            pg_session,
            email="presence@c.ma",
            slug="dr-presence",
            district="Errahma",
            claim=ClaimStatus.CLAIMED,
        )
        pg_session.add(DoctorLanding(doctor_id=doctor_id, is_personalized=True))
        pg_session.commit()

        board = ProspectController.board(now=NOW)

        (row,) = board.rows
        assert row.is_personalized is True
        assert row.plan == PLAN_LANDING
        assert board.presence == 1
        assert board.paying == 0

    def test_a_delisted_doctor_never_appears(self, pg_session: Session) -> None:
        """A removal is a tombstone; it must not resurface as a prospect."""
        _doctor(
            pg_session,
            email="gone@c.ma",
            slug="dr-gone",
            district="Errahma",
            claim=ClaimStatus.REMOVAL_REQUESTED,
        )

        assert ProspectController.board(now=NOW).rows == []

    def test_filters_narrow_the_list(self, pg_session: Session) -> None:
        _doctor(pg_session, email="a@c.ma", slug="dr-a", district="Errahma")
        _doctor(pg_session, email="b@c.ma", slug="dr-b", district="Maârif")
        _doctor(
            pg_session,
            email="c@c.ma",
            slug="dr-c",
            district="Errahma",
            claim=ClaimStatus.CLAIMED,
        )

        assert len(ProspectController.board(district="Errahma", now=NOW).rows) == 2
        assert len(ProspectController.board(onboarded=False, now=NOW).rows) == 2
        assert len(ProspectController.board(plan=PLAN_LANDING, now=NOW).rows) == 3
        assert ProspectController.board(now=NOW).districts == ["Errahma", "Maârif"]

    def test_ordered_by_district_so_the_list_reads_as_a_route(self, pg_session: Session) -> None:
        _doctor(pg_session, email="z@c.ma", slug="zed-maarif", district="Maârif")
        _doctor(pg_session, email="y@c.ma", slug="bee-errahma", district="Errahma")
        _doctor(pg_session, email="x@c.ma", slug="ay-errahma", district="Errahma")

        districts = [r.district for r in ProspectController.board(now=NOW).rows]

        assert districts == ["Errahma", "Errahma", "Maârif"]

    def test_flags_who_still_needs_a_real_pin(self, pg_session: Session) -> None:
        """The visit is the chance to fix a pin, so the list says so beforehand."""
        _doctor(pg_session, email="nopin@c.ma", slug="dr-nopin", district="Errahma")
        _doctor(
            pg_session,
            email="haspin@c.ma",
            slug="dr-haspin",
            district="Maârif",
            geo=True,
        )

        rows = {r.slug: r for r in ProspectController.board(now=NOW).rows}

        assert rows["dr-nopin"].needs_pin is True
        assert rows["dr-haspin"].needs_pin is False

    def test_a_geocoded_centroid_still_needs_a_pin(self, pg_session: Session) -> None:
        """Having coordinates is not the same as having usable ones."""
        uid = _doctor(
            pg_session, email="approx@c.ma", slug="dr-approx", district="Errahma", geo=True
        )
        with pg_session.begin():
            pg_session.execute(
                update(DoctorProfile)
                .where(DoctorProfile.user_id == uid)
                .values(geo_precision=GeoPrecision.APPROXIMATE)
            )

        (row,) = ProspectController.board(now=NOW).rows

        assert row.lat is not None
        assert row.needs_pin is True
