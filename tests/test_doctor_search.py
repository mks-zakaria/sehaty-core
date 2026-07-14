"""Doctor geo-search + ranking integration tests against a live PostGIS.

These exercise the real ``geopoint`` geography column: ``WKTElement`` writes,
``ST_DWithin``/``ST_Distance`` filtering and the ``<->`` nearest-neighbour
ordering — none of which stock SQLite can compile. They take the ``pg_session``
fixture (see ``conftest.py``) and SKIP when no database is reachable.

The scenario seeds four VERIFIED cardiologists at known distances from a fixed
origin (one of them just outside the radius), plus a dermatologist and a PENDING
cardiologist that must never surface, then asserts filtering, distances and —
crucially — that flipping ``RankingWeights.w_rating`` re-orders a far, highly
rated doctor above a near, poorly rated one.
"""

import pytest
from geoalchemy2.elements import WKTElement
from sehaty.db import (
    DoctorProfile,
    DoctorSpecialty,
    ReputationScore,
    Specialty,
    User,
    UserRole,
    VerificationStatus,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from sehaty.core.controllers.doctor_search import DoctorSearchController
from sehaty.core.errors import SehatyValidationError

# Fixed search origin (Casablanca-ish, lon/lat WGS84).
_LAT = 33.5731104
_LNG = -7.5898434
_SRID = 4326
_RADIUS_M = 10_000


def _seed_specialty(session: Session, slug: str) -> int:
    spec = Specialty(slug=slug, name_en=slug, name_fr=slug, name_ar=slug)
    session.add(spec)
    session.commit()
    return spec.id


def _seed_doctor(
    session: Session,
    *,
    email: str,
    slug: str,
    lat_offset: float,
    specialty_id: int,
    status: VerificationStatus = VerificationStatus.VERIFIED,
    avg_stars: float | None = None,
    review_count: int = 0,
) -> int:
    """Create a DOCTOR user + profile at ``_LAT + lat_offset`` and return uid.

    ~111 km per degree of latitude, so ``lat_offset`` maps predictably to metres
    of distance due north of the origin.
    """
    user = User(email=email, role=UserRole.DOCTOR, is_active=True)
    session.add(user)
    session.commit()

    profile = DoctorProfile(
        user_id=user.id,
        full_name=slug.replace("-", " ").title(),
        slug=slug,
        license_no=f"LIC-{user.id}",
        city="Casablanca",
        geopoint=WKTElement(f"POINT({_LNG} {_LAT + lat_offset})", srid=_SRID),
        verification_status=status,
    )
    session.add(profile)
    session.add(DoctorSpecialty(doctor_id=user.id, specialty_id=specialty_id))
    if avg_stars is not None:
        session.add(
            ReputationScore(user_id=user.id, avg_stars=avg_stars, review_count=review_count)
        )
    session.commit()
    return user.id


def _clear_weights(session: Session) -> None:
    """Reset ranking weights to "unset" so the controller uses its defaults.

    ``pg_session`` only truncates users/specialties, and ``ranking_weights`` has
    no FK to them, so a row written by one test would otherwise leak into the
    next run's Postgres and skew the default-weights assertions.
    """
    session.execute(text("TRUNCATE ranking_weights RESTART IDENTITY"))
    session.commit()


def _seed_scenario(session: Session) -> int:
    """Seed the full fixture set; return the cardiology specialty id."""
    _clear_weights(session)
    cardiology = _seed_specialty(session, "cardiology")
    dermatology = _seed_specialty(session, "dermatology")

    # Three VERIFIED cardiologists, increasing distance, differing reputations.
    _seed_doctor(
        session,
        email="near-low@clinic.ma",
        slug="dr-near-low",
        lat_offset=0.005,  # ~556 m
        specialty_id=cardiology,
        avg_stars=2.0,
        review_count=10,
    )
    _seed_doctor(
        session,
        email="mid-mid@clinic.ma",
        slug="dr-mid-mid",
        lat_offset=0.02,  # ~2.2 km
        specialty_id=cardiology,
        avg_stars=3.5,
        review_count=20,
    )
    _seed_doctor(
        session,
        email="far-high@clinic.ma",
        slug="dr-far-high",
        lat_offset=0.07,  # ~7.8 km (inside radius)
        specialty_id=cardiology,
        avg_stars=5.0,
        review_count=200,
    )

    # Excluded: outside the radius.
    _seed_doctor(
        session,
        email="outside@clinic.ma",
        slug="dr-outside",
        lat_offset=0.15,  # ~16.7 km (outside 10 km radius)
        specialty_id=cardiology,
        avg_stars=5.0,
        review_count=500,
    )
    # Excluded: right on top of the origin but a DIFFERENT specialty.
    _seed_doctor(
        session,
        email="derm@clinic.ma",
        slug="dr-derm",
        lat_offset=0.001,
        specialty_id=dermatology,
        avg_stars=5.0,
        review_count=99,
    )
    # Excluded: cardiologist nearby but PENDING (unverified).
    _seed_doctor(
        session,
        email="pending@clinic.ma",
        slug="dr-pending",
        lat_offset=0.001,
        specialty_id=cardiology,
        status=VerificationStatus.PENDING,
        avg_stars=5.0,
        review_count=99,
    )
    return cardiology


def test_search_filters_to_verified_in_radius_of_specialty(pg_session: Session) -> None:
    _seed_scenario(pg_session)

    results = DoctorSearchController.search(
        specialty_slug="cardiology", lat=_LAT, lng=_LNG, radius_m=_RADIUS_M
    )

    slugs = {r.slug for r in results}
    # Only the three VERIFIED cardiologists inside the radius; the outside /
    # dermatology / PENDING doctors are all excluded.
    assert slugs == {"dr-near-low", "dr-mid-mid", "dr-far-high"}

    by_slug = {r.slug: r for r in results}
    # Distances are plausible metres, nearest doctor genuinely nearest.
    assert 0 < by_slug["dr-near-low"].distance_m < 1_000
    assert 2_000 < by_slug["dr-mid-mid"].distance_m < 3_000
    assert 7_000 < by_slug["dr-far-high"].distance_m < 8_500
    # Coordinates round-tripped back out of the geography column.
    assert by_slug["dr-near-low"].lat == pytest.approx(_LAT + 0.005, abs=1e-6)
    assert by_slug["dr-near-low"].lng == pytest.approx(_LNG, abs=1e-6)


def test_default_weights_rank_near_low_above_far_high(pg_session: Session) -> None:
    _seed_scenario(pg_session)

    results = DoctorSearchController.search(
        specialty_slug="cardiology", lat=_LAT, lng=_LNG, radius_m=_RADIUS_M
    )
    rank = {r.slug: r.rank for r in results}

    # With balanced default weights the near (but poorly rated) doctor edges out
    # the far (but 5-star) one — proximity still carries real weight, so a top
    # rating alone is not enough to overcome ~7 km of extra distance.
    assert rank["dr-near-low"] > rank["dr-far-high"]
    # Results are sorted by rank descending.
    assert [r.rank for r in results] == sorted((r.rank for r in results), reverse=True)


def test_raising_w_rating_reorders_far_high_doctor_to_top(pg_session: Session) -> None:
    _seed_scenario(pg_session)

    # Sanity: with defaults the near 2-star doctor out-ranks the far 5-star one.
    before = DoctorSearchController.search(
        specialty_slug="cardiology", lat=_LAT, lng=_LNG, radius_m=_RADIUS_M
    )
    before_rank = {r.slug: r.rank for r in before}
    assert before_rank["dr-near-low"] > before_rank["dr-far-high"]
    assert before[0].slug != "dr-far-high"

    # Admin cranks the rating weight way up (single RankingWeights row).
    from sehaty.db import RankingWeights

    pg_session.add(RankingWeights(w_rating=10.0, w_distance=1.0, w_verified=0.5, w_recency=0.0))
    pg_session.commit()

    after = DoctorSearchController.search(
        specialty_slug="cardiology", lat=_LAT, lng=_LNG, radius_m=_RADIUS_M
    )
    # Rating now dominates: the far 5-star doctor jumps above the near 2-star one.
    assert after[0].slug == "dr-far-high"
    rank = {r.slug: r.rank for r in after}
    assert rank["dr-far-high"] > rank["dr-near-low"]


def test_doctor_outside_radius_excluded(pg_session: Session) -> None:
    cardiology = _seed_specialty(pg_session, "cardiology")
    _seed_doctor(
        pg_session,
        email="far@clinic.ma",
        slug="dr-way-out",
        lat_offset=0.2,  # ~22 km
        specialty_id=cardiology,
        avg_stars=5.0,
    )

    results = DoctorSearchController.search(
        specialty_slug="cardiology", lat=_LAT, lng=_LNG, radius_m=_RADIUS_M
    )
    assert results == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"specialty_slug": "cardiology", "lat": 95.0, "lng": _LNG},
        {"specialty_slug": "cardiology", "lat": _LAT, "lng": 200.0},
        {"specialty_slug": "cardiology", "lat": _LAT, "lng": _LNG, "radius_m": 0},
        {"specialty_slug": "cardiology", "lat": _LAT, "lng": _LNG, "radius_m": 999_999},
        {"specialty_slug": " ", "lat": _LAT, "lng": _LNG},
        {"specialty_slug": "cardiology", "lat": _LAT, "lng": _LNG, "limit": 0},
    ],
)
def test_invalid_inputs_raise(pg_session: Session, kwargs: dict) -> None:
    with pytest.raises(SehatyValidationError):
        DoctorSearchController.search(**kwargs)
