"""Pure-SQLite tests for landing-page analytics ingest and rollup.

Ingest is deliberately forgiving — a beacon from a patient's phone must never
turn into a visible error on a doctor's public page — so most of these assert
that bad input is *dropped*, not raised. Same dialect-scoped ``Geography`` shim
as the other SQLite suites.
"""

from datetime import UTC, datetime, timedelta

import pytest
from geoalchemy2 import Geography
from geoalchemy2 import functions as geo_functions
from sehaty.db import (
    DoctorProfile,
    LandingEvent,
    LandingEventType,
    User,
    UserRole,
    VerificationStatus,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.landing_analytics import LandingAnalyticsController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyValidationError


@compiles(Geography, "sqlite")
def _geography_as_text_on_sqlite(element, compiler, **kw) -> str:
    return "TEXT"


@compiles(geo_functions.ST_GeogFromText, "sqlite")
def _geog_bind_passthrough_on_sqlite(element, compiler, **kw) -> str:
    return compiler.process(list(element.clauses)[0], **kw)


@compiles(geo_functions.ST_AsEWKB, "sqlite")
@compiles(geo_functions.ST_AsBinary, "sqlite")
def _read_geopoint_passthrough_on_sqlite(element, compiler, **kw) -> str:
    return compiler.process(list(element.clauses)[0], **kw)


_TABLES = [User.__table__, DoctorProfile.__table__, LandingEvent.__table__]


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _doctor(
    factory: sessionmaker[Session], slug: str, *, verified: bool = True, active: bool = True
) -> int:
    with factory() as s:
        user = User(email=f"{slug}@clinic.ma", role=UserRole.DOCTOR, is_active=active)
        s.add(user)
        s.flush()
        s.add(
            DoctorProfile(
                user_id=user.id,
                full_name=slug,
                slug=slug,
                license_no=f"LIC-{slug}",
                verification_status=(
                    VerificationStatus.VERIFIED if verified else VerificationStatus.PENDING
                ),
            )
        )
        s.commit()
        return int(user.id)


class TestRecord:
    def test_records_an_event_for_a_published_doctor(self, db: sessionmaker[Session]) -> None:
        doctor_id = _doctor(db, "dr-ok")

        assert LandingAnalyticsController.record(
            doctor_slug="dr-ok", event_type="CALL_CLICK", source="qr"
        )

        with db() as s:
            row = s.execute(select(LandingEvent)).scalar_one()
        assert row.doctor_id == doctor_id
        assert row.type == LandingEventType.CALL_CLICK
        assert row.source == "qr"

    def test_drops_an_unknown_slug_without_raising(self, db: sessionmaker[Session]) -> None:
        # A 500 here would surface as a broken public page to a patient.
        assert (
            LandingAnalyticsController.record(doctor_slug="nobody", event_type="PAGE_VIEW") is False
        )

    def test_drops_an_unknown_event_type(self, db: sessionmaker[Session]) -> None:
        _doctor(db, "dr-type")
        assert (
            LandingAnalyticsController.record(doctor_slug="dr-type", event_type="LOL_CLICK")
            is False
        )

    @pytest.mark.parametrize(
        ("verified", "active"), [(False, True), (True, False)], ids=["pending", "deactivated"]
    )
    def test_drops_events_for_unpublished_doctors(
        self, db: sessionmaker[Session], verified: bool, active: bool
    ) -> None:
        _doctor(db, "dr-hidden", verified=verified, active=active)
        assert (
            LandingAnalyticsController.record(doctor_slug="dr-hidden", event_type="PAGE_VIEW")
            is False
        )

    def test_normalizes_an_unrecognized_source(self, db: sessionmaker[Session]) -> None:
        # Prevents a caller smuggling a search query — health data — into the column.
        _doctor(db, "dr-src")
        LandingAnalyticsController.record(
            doctor_slug="dr-src",
            event_type="PAGE_VIEW",
            source="https://google.com/search?q=dermatologue+psoriasis",
        )
        with db() as s:
            assert s.execute(select(LandingEvent.source)).scalar_one() == "other"

    def test_blank_source_is_stored_as_null(self, db: sessionmaker[Session]) -> None:
        _doctor(db, "dr-blank")
        LandingAnalyticsController.record(
            doctor_slug="dr-blank", event_type="PAGE_VIEW", source="  "
        )
        with db() as s:
            assert s.execute(select(LandingEvent.source)).scalar_one() is None


class TestCounts:
    def _seed_events(self, factory: sessionmaker[Session], doctor_id: int) -> None:
        now = datetime.now(UTC)
        rows = [
            (LandingEventType.PAGE_VIEW, now - timedelta(days=1)),
            (LandingEventType.PAGE_VIEW, now - timedelta(days=2)),
            (LandingEventType.CALL_CLICK, now - timedelta(days=3)),
            (LandingEventType.WHATSAPP_CLICK, now - timedelta(days=4)),
            (LandingEventType.BOOK_CLICK, now - timedelta(days=5)),
            # Outside a 30-day window — must not be counted.
            (LandingEventType.PAGE_VIEW, now - timedelta(days=90)),
        ]
        with factory() as s:
            for event_type, occurred_at in rows:
                s.add(LandingEvent(doctor_id=doctor_id, type=event_type, occurred_at=occurred_at))
            s.commit()

    def test_counts_within_the_window(self, db: sessionmaker[Session]) -> None:
        doctor_id = _doctor(db, "dr-counts")
        self._seed_events(db, doctor_id)

        counts = LandingAnalyticsController.counts_for_doctor(doctor_id, days=30)

        assert counts.page_views == 2  # the 90-day-old view is excluded
        assert counts.call_clicks == 1
        assert counts.whatsapp_clicks == 1
        assert counts.book_clicks == 1
        assert counts.directions_clicks == 0

    def test_wider_window_includes_older_events(self, db: sessionmaker[Session]) -> None:
        doctor_id = _doctor(db, "dr-wide")
        self._seed_events(db, doctor_id)
        assert LandingAnalyticsController.counts_for_doctor(doctor_id, days=365).page_views == 3

    def test_total_intent_sums_the_reach_out_actions(self, db: sessionmaker[Session]) -> None:
        # This is the number the upsell conversation actually turns on.
        doctor_id = _doctor(db, "dr-intent")
        self._seed_events(db, doctor_id)
        assert LandingAnalyticsController.counts_for_doctor(doctor_id).total_intent == 3

    def test_a_doctor_with_no_events_reports_zeroes(self, db: sessionmaker[Session]) -> None:
        doctor_id = _doctor(db, "dr-quiet")
        counts = LandingAnalyticsController.counts_for_doctor(doctor_id)
        assert counts.page_views == 0
        assert counts.total_intent == 0

    def test_events_are_not_mixed_between_doctors(self, db: sessionmaker[Session]) -> None:
        first = _doctor(db, "dr-one")
        second = _doctor(db, "dr-two")
        self._seed_events(db, first)

        assert LandingAnalyticsController.counts_for_doctor(second).page_views == 0

    @pytest.mark.parametrize("days", [0, -1, 400])
    def test_rejects_an_out_of_range_window(self, db: sessionmaker[Session], days: int) -> None:
        doctor_id = _doctor(db, "dr-range")
        with pytest.raises(SehatyValidationError):
            LandingAnalyticsController.counts_for_doctor(doctor_id, days=days)
