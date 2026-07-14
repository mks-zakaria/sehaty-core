"""Doctor geo-search + ranking business logic.

Class-as-namespace with ``@staticmethod`` (the RevlyMainDBClient pattern, same
as ``DoctorController``): validate inputs, raise the SehatyError taxonomy,
delegate IO to ``services.doctor_search``, then rank in-process.

Ranking
-------
The DB returns candidates nearest-first; ranking re-orders them by a weighted
blend of rating, proximity and verification. Weights are admin-tunable via
``ConfigController.get_ranking_weights()`` (backed by the single
``RankingWeights`` row, falling back to sane defaults when unset):

    rank = w_rating   * (avg_stars / 5)
         + w_distance * (1 - min(distance_m / radius_m, 1))
         + w_verified * 1.0

The pure formula lives in ``score_doctor`` so it can be exercised without a live
geo database. Every returned doctor is VERIFIED (filtered in the query), so the
verified term is a constant ``w_verified`` today; it is kept explicit so a
future mixed-trust result set ranks correctly without reshaping the formula.

TODO(recency): fold in a ``w_recency`` term driven by the doctor's most recent
completed appointment (freshly-active doctors rank higher). Held at 0 until the
scheduling read lands so the score stays deterministic.
"""

from dataclasses import dataclass

from sehaty.core.controllers.config import ConfigController, RankingWeightsValues
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyValidationError
from sehaty.core.services import doctor_search as search_service

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_MIN_RADIUS_M = 1
_MAX_RADIUS_M = 50_000


def score_doctor(
    *,
    avg_stars: float,
    distance_m: float,
    radius_m: float,
    weights: RankingWeightsValues,
) -> float:
    """Weighted ranking score for a single VERIFIED doctor hit.

    Pure function of the reputation/proximity inputs and the admin weights, so
    the ranking maths is unit-testable without touching PostGIS. Higher is
    better. The verified term is a constant today (every hit is VERIFIED); see
    the module docstring for the held-at-zero recency term.
    """
    return (
        weights.w_rating * (avg_stars / 5)
        + weights.w_distance * (1 - min(distance_m / radius_m, 1))
        + weights.w_verified * 1.0
        # + weights.w_recency * recency_term  # TODO(recency): see module docstring.
    )


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

        # Admin-tunable weights, fetched once per search; defaults preserve the
        # historical ranking when no RankingWeights row has been written.
        weights = ConfigController.get_ranking_weights()

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
                rank=score_doctor(
                    avg_stars=row.avg_stars,
                    distance_m=row.distance_m,
                    radius_m=radius_m,
                    weights=weights,
                ),
            )
            for row in rows
        ]
        results.sort(key=lambda r: (-r.rank, r.distance_m))
        return results
