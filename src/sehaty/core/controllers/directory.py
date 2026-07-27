"""Public doctor-directory read (browse by specialty + rating, no geolocation).

The non-geo companion to ``DoctorSearchController``: same eligibility (only
VERIFIED, active doctors surface) and the same reputation join for
``avg_stars``/``review_count``, but ordered by rating/reviews/name instead of
proximity — so a patient can browse accredited doctors without sharing a
location.

Class-as-namespace with ``@staticmethod`` (the RevlyMainDBClient pattern), one
transaction via ``get_session()``. Reads use a **column-only** projection so the
PostGIS ``geopoint`` geography blob on ``DoctorProfile`` is never selected — the
query compiles on stock SQLite (tests) as well as Postgres. Reputation is an
``outerjoin`` with ``avg_stars``/``review_count`` coalesced to zero so a
review-less doctor still appears (rating term zero). Each page's specialty names
are resolved in a single follow-up query keyed by doctor id (no N+1).
"""

from sehaty.db import (
    DoctorProfile,
    DoctorSpecialty,
    ReputationScore,
    Specialty,
    User,
    VerificationStatus,
)
from sqlalchemy import func, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyValidationError
from sehaty.core.places import match_display_names

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_VALID_SORTS = ("rating", "reviews", "name")


class DirectoryRow(DomainModel):
    """One doctor in the directory listing (no geo)."""

    slug: str
    full_name: str
    photo_url: str | None
    city: str | None
    district: str | None
    consultation_fee: float | None
    avg_stars: float
    review_count: int
    specialties: list[str]


class DirectoryPage(DomainModel):
    """A page of directory rows plus the total match count for pagination."""

    total: int
    doctors: list[DirectoryRow]


class DoctorDirectoryController:
    @staticmethod
    def list_directory(
        specialty: str | None = None,
        query: str | None = None,
        city: str | None = None,
        district: str | None = None,
        sort: str = "rating",
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> DirectoryPage:
        """Browse VERIFIED, active doctors by specialty and rating (no geo).

        ``specialty`` is an optional specialty *slug* that narrows the listing to
        doctors carrying that specialty.

        ``city`` and ``district`` are place *slugs* (``"casablanca"``,
        ``"maarif"``) as they appear in the public URL. They are resolved back to
        the display spellings actually stored — matching every variant, so a
        ``city`` recorded as both "Casablanca" and "casablanca" browses as one
        page. A slug that matches nothing yields an empty page rather than an
        error: an unknown city is a 404 concern for the route, not a validation
        failure here.

        ``sort`` is one of:

        * ``"rating"`` (default) — ``avg_stars`` desc (review-less doctors, whose
          score coalesces to 0, sort last), then ``review_count`` desc;
        * ``"reviews"`` — ``review_count`` desc;
        * ``"name"`` — ``full_name`` asc.

        Returns a :class:`DirectoryPage` whose ``total`` counts every matching
        doctor (for pagination) and whose ``doctors`` is the ``limit``/``offset``
        slice. Ties break by ``user_id`` so paging is stable.
        """
        if sort not in _VALID_SORTS:
            raise SehatyValidationError(f"sort must be one of {_VALID_SORTS}, got {sort!r}")
        if limit <= 0 or limit > _MAX_LIMIT:
            raise SehatyValidationError(f"limit out of range: {limit}")
        if offset < 0:
            raise SehatyValidationError(f"offset out of range: {offset}")

        specialty_slug = specialty.strip() if specialty else None

        avg_stars = func.coalesce(ReputationScore.avg_stars, 0.0).label("avg_stars")
        review_count = func.coalesce(ReputationScore.review_count, 0).label("review_count")

        # Column-only projection (never the geopoint geography blob) + the
        # eligibility filter shared with the geo search.
        stmt = (
            select(
                DoctorProfile.user_id,
                DoctorProfile.slug,
                DoctorProfile.full_name,
                DoctorProfile.photo_url,
                DoctorProfile.city,
                DoctorProfile.district,
                DoctorProfile.consultation_fee,
                avg_stars,
                review_count,
            )
            .join(User, User.id == DoctorProfile.user_id)
            .outerjoin(ReputationScore, ReputationScore.user_id == DoctorProfile.user_id)
            .where(
                User.is_active.is_(True),
                DoctorProfile.verification_status == VerificationStatus.VERIFIED,
            )
        )

        # Count query mirrors the filters (no ordering/paging) for `total`.
        count_stmt = (
            select(func.count())
            .select_from(DoctorProfile)
            .join(User, User.id == DoctorProfile.user_id)
            .where(
                User.is_active.is_(True),
                DoctorProfile.verification_status == VerificationStatus.VERIFIED,
            )
        )

        if specialty_slug:
            stmt = (
                stmt.join(DoctorSpecialty, DoctorSpecialty.doctor_id == DoctorProfile.user_id)
                .join(Specialty, Specialty.id == DoctorSpecialty.specialty_id)
                .where(Specialty.slug == specialty_slug)
            )
            count_stmt = (
                count_stmt.join(DoctorSpecialty, DoctorSpecialty.doctor_id == DoctorProfile.user_id)
                .join(Specialty, Specialty.id == DoctorSpecialty.specialty_id)
                .where(Specialty.slug == specialty_slug)
            )

        # Narrow to a doctor name (the "type then name" search) — case-insensitive.
        name_q = query.strip() if query and query.strip() else None
        if name_q:
            like = f"%{name_q}%"
            stmt = stmt.where(DoctorProfile.full_name.ilike(like))
            count_stmt = count_stmt.where(DoctorProfile.full_name.ilike(like))

        if sort == "rating":
            # Coalesced score desc puts review-less (0) doctors last without a
            # dialect-specific NULLS LAST clause (SQLite-safe).
            stmt = stmt.order_by(avg_stars.desc(), review_count.desc(), DoctorProfile.user_id.asc())
        elif sort == "reviews":
            stmt = stmt.order_by(review_count.desc(), DoctorProfile.user_id.asc())
        else:  # "name"
            stmt = stmt.order_by(DoctorProfile.full_name.asc(), DoctorProfile.user_id.asc())

        with get_session() as session:
            # Place filters resolve slug -> stored spellings, which needs the DB;
            # applied before paging so `total` reflects them.
            for column, slug in ((DoctorProfile.city, city), (DoctorProfile.district, district)):
                if not (slug and slug.strip()):
                    continue
                stored = session.execute(select(column).distinct()).scalars().all()
                matches = match_display_names(slug.strip(), list(stored))
                if not matches:
                    # Nothing stored under that slug — an empty page, not an error.
                    return DirectoryPage(total=0, doctors=[])
                stmt = stmt.where(column.in_(matches))
                count_stmt = count_stmt.where(column.in_(matches))

            total = int(session.execute(count_stmt).scalar_one())
            rows = session.execute(stmt.limit(limit).offset(offset)).all()
            specialties_by_doctor = _resolve_specialties(session, [row.user_id for row in rows])

        doctors = [
            DirectoryRow(
                slug=row.slug,
                full_name=row.full_name,
                photo_url=row.photo_url,
                city=row.city,
                district=row.district,
                consultation_fee=row.consultation_fee,
                avg_stars=float(row.avg_stars),
                review_count=int(row.review_count),
                specialties=specialties_by_doctor.get(row.user_id, []),
            )
            for row in rows
        ]
        return DirectoryPage(total=total, doctors=doctors)


def _resolve_specialties(session, doctor_ids: list[int]) -> dict[int, list[str]]:  # noqa: ANN001
    """Map ``doctor_id -> [specialty display name]`` in one query (no N+1).

    Uses ``name_fr`` (the French display name — Morocco's primary UI language)
    and orders names alphabetically so each doctor's list is deterministic.
    """
    if not doctor_ids:
        return {}

    rows = session.execute(
        select(DoctorSpecialty.doctor_id, Specialty.name_fr)
        .join(Specialty, Specialty.id == DoctorSpecialty.specialty_id)
        .where(DoctorSpecialty.doctor_id.in_(doctor_ids))
        .order_by(Specialty.name_fr.asc())
    ).all()

    by_doctor: dict[int, list[str]] = {}
    for doctor_id, name in rows:
        by_doctor.setdefault(doctor_id, []).append(name)
    return by_doctor
