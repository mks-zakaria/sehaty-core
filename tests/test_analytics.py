"""Doctor practice-insights analytics tests on an in-memory SQLite engine.

Covers the rolling-window monthly appointment buckets (counts + estimated
revenue), the window no-show rate and totals, the reputation read, and the
published-review trend — plus doctor scoping and the empty-doctor shape. The
doctor-profile table carries the PostGIS ``geopoint`` column, which stock SQLite
cannot compile, so a tiny ``Geography -> TEXT`` compiler shim is registered for
the ``sqlite`` dialect — the analytics queries never touch ``geopoint`` itself,
only ``consultation_fee`` alongside it.
"""

from datetime import UTC, datetime

import pytest
from geoalchemy2 import Geography
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    DoctorProfile,
    PatientProfile,
    ReputationScore,
    Review,
    ReviewDirection,
    ReviewStatus,
    User,
    UserRole,
    VerificationStatus,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, event
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.analytics import DoctorAnalyticsController
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
    ReputationScore.__table__,
]

# Anchor "now" mid-July so the 6-month window is 2026-02 .. 2026-07.
_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
_FEE = 300.0


def _dt(year: int, month: int, day: int = 15) -> datetime:
    return datetime(year, month, day, 10, 0, tzinfo=UTC)


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # GeoAlchemy2 wraps geopoint writes in ST_GeogFromText(); register a no-op
    # SQLite UDF so inserts of NULL geopoints round-trip without a live PostGIS.
    @event.listens_for(engine, "connect")
    def _register_geog_udf(dbapi_conn, _record) -> None:  # noqa: ANN001
        dbapi_conn.create_function("ST_GeogFromText", 1, lambda value: value)

    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _add_appt(
    s: Session, doctor_id: int, patient_id: int, when: datetime, status: AppointmentStatus
) -> None:
    s.add(
        Appointment(
            patient_id=patient_id,
            doctor_id=doctor_id,
            start_at=when,
            end_at=when,
            status=status,
        )
    )


def _add_review(
    s: Session,
    target_id: int,
    author_id: int,
    appointment_id: int,
    when: datetime,
    *,
    status: ReviewStatus = ReviewStatus.PUBLISHED,
    direction: ReviewDirection = ReviewDirection.PATIENT_ON_DOCTOR,
) -> None:
    r = Review(
        author_id=author_id,
        target_id=target_id,
        appointment_id=appointment_id,
        direction=direction,
        stars=5,
        status=status,
    )
    s.add(r)
    s.flush()
    # created_at is a TimestampMixin server/default column; force the bucket month.
    r.created_at = when
    s.flush()


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> dict[str, int]:
    """A doctor with mixed-status appointments across 3 recent months + reviews.

    Layout (fee = 300):
      * 2026-05: 2 completed, 1 no_show, 1 cancelled, 1 upcoming (CONFIRMED)
      * 2026-06: 1 completed, 1 no_show
      * 2026-07: 3 completed
    Another doctor's data (appointments + a review + a score) must be excluded.
    An out-of-window appointment (2026-01) must be excluded.
    """
    ids: dict[str, int] = {}
    with db() as s:
        doc = User(email="doc@clinic.ma", role=UserRole.DOCTOR, is_active=True)
        other = User(email="other@clinic.ma", role=UserRole.DOCTOR, is_active=True)
        patient = User(email="pat@sehaty.ma", role=UserRole.PATIENT, is_active=True)
        s.add_all([doc, other, patient])
        s.flush()
        ids.update(doc=doc.id, other=other.id, patient=patient.id)

        s.add_all(
            [
                DoctorProfile(
                    user_id=doc.id,
                    full_name="Dr Amine",
                    slug="dr-amine",
                    license_no="LIC-1",
                    consultation_fee=_FEE,
                    verification_status=VerificationStatus.VERIFIED,
                ),
                DoctorProfile(
                    user_id=other.id,
                    full_name="Dr Sara",
                    slug="dr-sara",
                    license_no="LIC-2",
                    consultation_fee=500.0,
                    verification_status=VerificationStatus.VERIFIED,
                ),
                PatientProfile(user_id=patient.id, full_name="Youssef"),
            ]
        )

        # --- doc: 2026-05 ---
        _add_appt(s, doc.id, patient.id, _dt(2026, 5, 3), AppointmentStatus.COMPLETED)
        _add_appt(s, doc.id, patient.id, _dt(2026, 5, 10), AppointmentStatus.COMPLETED)
        _add_appt(s, doc.id, patient.id, _dt(2026, 5, 12), AppointmentStatus.NO_SHOW)
        _add_appt(s, doc.id, patient.id, _dt(2026, 5, 20), AppointmentStatus.CANCELLED)
        _add_appt(s, doc.id, patient.id, _dt(2026, 5, 25), AppointmentStatus.CONFIRMED)
        # --- doc: 2026-06 ---
        _add_appt(s, doc.id, patient.id, _dt(2026, 6, 5), AppointmentStatus.COMPLETED)
        _add_appt(s, doc.id, patient.id, _dt(2026, 6, 18), AppointmentStatus.NO_SHOW)
        # --- doc: 2026-07 ---
        _add_appt(s, doc.id, patient.id, _dt(2026, 7, 1), AppointmentStatus.COMPLETED)
        _add_appt(s, doc.id, patient.id, _dt(2026, 7, 8), AppointmentStatus.COMPLETED)
        _add_appt(s, doc.id, patient.id, _dt(2026, 7, 14), AppointmentStatus.COMPLETED)
        # --- doc: out of window (2026-01), must be excluded ---
        _add_appt(s, doc.id, patient.id, _dt(2026, 1, 9), AppointmentStatus.COMPLETED)

        # --- other doctor's appointments (must be excluded from doc's view) ---
        _add_appt(s, other.id, patient.id, _dt(2026, 6, 6), AppointmentStatus.COMPLETED)
        _add_appt(s, other.id, patient.id, _dt(2026, 7, 7), AppointmentStatus.NO_SHOW)

        s.flush()
        # Distinct appointments per review to satisfy the (author, appointment,
        # direction) unique constraint; analytics only reads created_at/target/
        # direction/status, so the exact appointment linkage is immaterial.
        appt_ids = [a.id for a in s.query(Appointment).order_by(Appointment.id).all()]

        # Reviews targeting doc: published in 2026-05 and two in 2026-07; one
        # PENDING (not counted), one out-of-window published (2026-01), and one
        # doctor-on-patient direction (not counted).
        _add_review(s, doc.id, patient.id, appt_ids[0], _dt(2026, 5, 4))
        _add_review(s, doc.id, patient.id, appt_ids[1], _dt(2026, 7, 2))
        _add_review(s, doc.id, patient.id, appt_ids[2], _dt(2026, 7, 20))
        _add_review(
            s, doc.id, patient.id, appt_ids[3], _dt(2026, 6, 9), status=ReviewStatus.PENDING
        )
        _add_review(s, doc.id, patient.id, appt_ids[4], _dt(2026, 1, 9))
        _add_review(
            s,
            doc.id,
            patient.id,
            appt_ids[0],
            _dt(2026, 7, 3),
            direction=ReviewDirection.DOCTOR_ON_PATIENT,
        )
        # A published review targeting the OTHER doctor (must be excluded).
        _add_review(s, other.id, patient.id, appt_ids[5], _dt(2026, 7, 5))

        # Materialized reputation scores.
        s.add(ReputationScore(user_id=doc.id, avg_stars=4.5, review_count=8))
        s.add(ReputationScore(user_id=other.id, avg_stars=2.0, review_count=3))

        s.commit()
    return ids


def _by_month(analytics) -> dict[str, object]:  # noqa: ANN001
    return {m.month: m for m in analytics.by_month}


def test_month_buckets_and_estimated_revenue(seeded: dict[str, int]) -> None:
    a = DoctorAnalyticsController.doctor_analytics(seeded["doc"], months=6, now=_NOW)

    # Window is exactly the last 6 calendar months, oldest -> newest.
    assert [m.month for m in a.by_month] == [
        "2026-02",
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
    ]

    bm = _by_month(a)
    # May: total 5 (2 completed, 1 no_show, 1 cancelled, 1 confirmed/upcoming).
    assert (bm["2026-05"].total, bm["2026-05"].completed) == (5, 2)
    assert (bm["2026-05"].no_show, bm["2026-05"].cancelled) == (1, 1)
    assert bm["2026-05"].estimated_revenue == 2 * _FEE
    # June: 1 completed, 1 no_show.
    assert (bm["2026-06"].total, bm["2026-06"].completed, bm["2026-06"].no_show) == (2, 1, 1)
    assert bm["2026-06"].estimated_revenue == 1 * _FEE
    # July: 3 completed.
    assert (bm["2026-07"].total, bm["2026-07"].completed) == (3, 3)
    assert bm["2026-07"].estimated_revenue == 3 * _FEE
    # Quiet months are present and zeroed.
    for key in ("2026-02", "2026-03", "2026-04"):
        assert (bm[key].total, bm[key].completed, bm[key].estimated_revenue) == (0, 0, 0.0)


def test_window_totals_and_no_show_rate(seeded: dict[str, int]) -> None:
    a = DoctorAnalyticsController.doctor_analytics(seeded["doc"], months=6, now=_NOW)

    # Totals over the window (out-of-window Jan appt excluded).
    assert a.total_appointments == 5 + 2 + 3
    assert a.total_completed == 2 + 1 + 3  # 6
    assert a.total_no_show == 1 + 1  # 2
    # no_show_rate = 2 / (6 + 2) = 0.25.
    assert a.no_show_rate == pytest.approx(0.25)


def test_reputation_and_review_trend(seeded: dict[str, int]) -> None:
    a = DoctorAnalyticsController.doctor_analytics(seeded["doc"], months=6, now=_NOW)

    # From the materialized ReputationScore.
    assert a.avg_rating == 4.5
    assert a.review_count == 8

    # Published PATIENT_ON_DOCTOR reviews targeting doc: 1 in May, 2 in July.
    rbm = {r.month: r.count for r in a.reviews_by_month}
    assert [r.month for r in a.reviews_by_month] == [
        "2026-02",
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
    ]
    assert rbm["2026-05"] == 1
    assert rbm["2026-07"] == 2
    # PENDING (June), out-of-window (Jan), and doctor-on-patient are excluded.
    assert rbm["2026-06"] == 0


def test_doctor_scoping_excludes_others(seeded: dict[str, int]) -> None:
    a = DoctorAnalyticsController.doctor_analytics(seeded["doc"], months=6, now=_NOW)
    # The other doctor contributed a June completed + July no_show + a July
    # review + a score; none of it may leak into doc's numbers.
    assert a.total_appointments == 10
    assert a.total_no_show == 2
    assert a.avg_rating == 4.5  # doc's score, not other's 2.0
    assert sum(r.count for r in a.reviews_by_month) == 3  # not 4


def test_empty_doctor_zeros_with_present_buckets(db: sessionmaker[Session]) -> None:
    with db() as s:
        doc = User(email="lonely@clinic.ma", role=UserRole.DOCTOR, is_active=True)
        s.add(doc)
        s.flush()
        s.add(
            DoctorProfile(
                user_id=doc.id,
                full_name="Dr Nobody",
                slug="dr-nobody",
                license_no="LIC-9",
                # consultation_fee intentionally unset -> revenue defaults to 0.
            )
        )
        s.commit()
        doc_id = doc.id

    a = DoctorAnalyticsController.doctor_analytics(doc_id, months=6, now=_NOW)

    # All buckets present but zeroed, oldest -> newest.
    assert len(a.by_month) == 6
    assert [m.month for m in a.by_month] == [
        "2026-02",
        "2026-03",
        "2026-04",
        "2026-05",
        "2026-06",
        "2026-07",
    ]
    assert all(m.total == 0 and m.estimated_revenue == 0.0 for m in a.by_month)
    assert len(a.reviews_by_month) == 6
    assert all(r.count == 0 for r in a.reviews_by_month)

    assert a.total_appointments == 0
    assert a.total_completed == 0
    assert a.total_no_show == 0
    assert a.no_show_rate == 0.0
    # No ReputationScore row -> zeros.
    assert a.avg_rating == 0.0
    assert a.review_count == 0


def test_custom_window_length(seeded: dict[str, int]) -> None:
    # A 3-month window ends at July and starts at May.
    a = DoctorAnalyticsController.doctor_analytics(seeded["doc"], months=3, now=_NOW)
    assert [m.month for m in a.by_month] == ["2026-05", "2026-06", "2026-07"]
    # Feb..Apr excluded from totals (they were empty anyway); May..Jul retained.
    assert a.total_completed == 6
