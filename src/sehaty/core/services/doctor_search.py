"""Geo doctor-search IO layer.

The only place that knows about the PostGIS query. Pure IO: it takes a caller-
owned ``Session`` and returns plain rows, so the controller stays in charge of
sessions and of the (weights-driven) ranking maths.

Geo notes (GeoAlchemy2 over the ``geopoint`` Geography column, SRID 4326):

* The search origin is a ``WKTElement("POINT(lng lat)", srid=4326)`` — PostGIS
  lon-lat axis order, so longitude comes first (mirrors ``DoctorController``).
* ``ST_DWithin`` over ``Geography`` filters by great-circle metres directly.
* ``ST_Distance`` over ``Geography`` yields the distance in metres.
* Ordering uses the ``<->`` KNN operator so the GIST index on ``geopoint``
  drives a true nearest-neighbour scan.
* ``lat``/``lng`` are projected back to plain floats via ``ST_Y``/``ST_X`` over
  a ``cast(..., Geometry)`` — the raw geography blob is never surfaced.
"""

from dataclasses import dataclass

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKTElement
from sehaty.db import (
    DoctorProfile,
    DoctorSpecialty,
    ReputationScore,
    Specialty,
    User,
    VerificationStatus,
)
from sqlalchemy import cast, func, select
from sqlalchemy.orm import Session

_SRID = 4326
_HARD_LIMIT = 100


@dataclass(frozen=True)
class DoctorSearchRow:
    """One raw search hit, straight out of the DB (pre-ranking)."""

    slug: str
    full_name: str
    photo_url: str | None
    city: str | None
    distance_m: float
    lat: float | None
    lng: float | None
    avg_stars: float
    review_count: int


def search_doctors(
    session: Session,
    *,
    specialty_slug: str,
    lat: float,
    lng: float,
    radius_m: float,
    limit: int,
) -> list[DoctorSearchRow]:
    """Return VERIFIED, active doctors of ``specialty_slug`` within ``radius_m``.

    Nearest first (``geopoint <-> point``), capped at 100 rows regardless of the
    requested ``limit``. Missing reputation aggregates coalesce to zero so a
    doctor with no reviews still ranks (just with a zero rating term upstream).
    """
    point = WKTElement(f"POINT({lng} {lat})", srid=_SRID)

    stmt = (
        select(
            DoctorProfile.slug,
            DoctorProfile.full_name,
            DoctorProfile.photo_url,
            DoctorProfile.city,
            func.ST_Distance(DoctorProfile.geopoint, point).label("distance_m"),
            func.ST_Y(cast(DoctorProfile.geopoint, Geometry)).label("lat"),
            func.ST_X(cast(DoctorProfile.geopoint, Geometry)).label("lng"),
            func.coalesce(ReputationScore.avg_stars, 0.0).label("avg_stars"),
            func.coalesce(ReputationScore.review_count, 0).label("review_count"),
        )
        .join(User, User.id == DoctorProfile.user_id)
        .join(DoctorSpecialty, DoctorSpecialty.doctor_id == DoctorProfile.user_id)
        .join(Specialty, Specialty.id == DoctorSpecialty.specialty_id)
        .outerjoin(ReputationScore, ReputationScore.user_id == DoctorProfile.user_id)
        .where(
            User.is_active.is_(True),
            Specialty.slug == specialty_slug,
            DoctorProfile.verification_status.in_(VerificationStatus.publicly_visible()),
            func.ST_DWithin(DoctorProfile.geopoint, point, radius_m),
        )
        .order_by(DoctorProfile.geopoint.op("<->")(point))
        .limit(min(limit, _HARD_LIMIT))
    )

    return [
        DoctorSearchRow(
            slug=row.slug,
            full_name=row.full_name,
            photo_url=row.photo_url,
            city=row.city,
            distance_m=float(row.distance_m),
            lat=row.lat,
            lng=row.lng,
            avg_stars=float(row.avg_stars),
            review_count=int(row.review_count),
        )
        for row in session.execute(stmt).all()
    ]
