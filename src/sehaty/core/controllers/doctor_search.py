"""Doctor geo-search + ranking business logic.

Class-as-namespace with ``@staticmethod`` (the RevlyMainDBClient pattern, same
as ``DoctorController``): validate inputs, raise the SehatyError taxonomy,
delegate IO to ``services.doctor_search``, then rank in-process.

Ranking
-------
The DB returns candidates nearest-first; ranking re-orders them by a weighted
blend of rating, proximity and verification. Weights are admin-tunable via the
single ``RankingWeights`` row (falling back to sane defaults when unset):

    rank = w_rating   * (avg_stars / 5)
         + w_distance * (1 - min(distance_m / radius_m, 1))
         + w_verified * 1.0

Every returned doctor is VERIFIED (filtered in the query), so the verified term
is a constant ``w_verified`` today; it is kept explicit so a future mixed-trust
result set ranks correctly without reshaping the formula.

TODO(recency): fold in a ``w_recency`` term driven by the doctor's most recent
completed appointment (freshly-active doctors rank higher). Held at 0 until the
scheduling read lands so the score stays deterministic.
"""

from dataclasses import dataclass

from sehaty.db import RankingWeights
from sqlalchemy import select

from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyValidationError
from sehaty.core.services import doctor_search as search_service

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_MIN_RADIUS_M = 1
_MAX_RADIUS_M = 50_000

# Fallback weights when no RankingWeights row exists (see admin_config defaults).
_DEFAULT_W_RATING = 1.0
_DEFAULT_W_DISTANCE = 1.0
_DEFAULT_W_VERIFIED = 0.5
_DEFAULT_W_RECENCY = 0.25


@dataclass(frozen=True)
class DoctorSearchResult:
    """A ranked search hit ready for the transport layer."""

    slug: str
    full_name: str
    photo_url: str | None
    city: str | None
    distance_m: float
    lat: float | None
    lng: float | None
    avg_stars: float
    review_count: int
    rank: float


class DoctorSearchController:
    @staticmethod
    def search(
        *,
        specialty_slug: str,
        lat: float,
        lng: float,
        radius_m: float = 10_000,
        limit: int = _DEFAULT_LIMIT,
    ) -> list[DoctorSearchResult]:
        """Find VERIFIED doctors of a specialty near a point, ranked best-first.

        Validates the geo/paging inputs, runs the PostGIS search, then scores
        each hit with the admin weights and returns the list sorted by ``rank``
        descending (ties broken by ``distance_m`` ascending — nearer wins).
        """
        if not specialty_slug or not specialty_slug.strip():
            raise SehatyValidationError("specialty_slug is required")
        if not -90 <= lat <= 90:
            raise SehatyValidationError(f"lat out of range: {lat}")
        if not -180 <= lng <= 180:
            raise SehatyValidationError(f"lng out of range: {lng}")
        if not _MIN_RADIUS_M <= radius_m <= _MAX_RADIUS_M:
            raise SehatyValidationError(f"radius_m out of range: {radius_m}")
        if limit <= 0 or limit > _MAX_LIMIT:
            raise SehatyValidationError(f"limit out of range: {limit}")

        with get_session() as session:
            rows = search_service.search_doctors(
                session,
                specialty_slug=specialty_slug.strip(),
                lat=lat,
                lng=lng,
                radius_m=radius_m,
                limit=limit,
            )
            weights = session.execute(select(RankingWeights)).scalars().first()

        w_rating = weights.w_rating if weights else _DEFAULT_W_RATING
        w_distance = weights.w_distance if weights else _DEFAULT_W_DISTANCE
        w_verified = weights.w_verified if weights else _DEFAULT_W_VERIFIED

        results = [
            DoctorSearchResult(
                slug=row.slug,
                full_name=row.full_name,
                photo_url=row.photo_url,
                city=row.city,
                distance_m=row.distance_m,
                lat=row.lat,
                lng=row.lng,
                avg_stars=row.avg_stars,
                review_count=row.review_count,
                rank=(
                    w_rating * (row.avg_stars / 5)
                    + w_distance * (1 - min(row.distance_m / radius_m, 1))
                    + w_verified * 1.0
                    # + w_recency * recency_term  # TODO(recency): see module docstring.
                ),
            )
            for row in rows
        ]
        results.sort(key=lambda r: (-r.rank, r.distance_m))
        return results
