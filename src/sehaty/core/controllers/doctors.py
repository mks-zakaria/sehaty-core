"""Doctor business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern):
organized namespacing without forcing an instance-with-state. Validates
inputs, raises the SehatyError taxonomy, delegates IO to the service.

Geo notes (GeoAlchemy2 over the PostGIS ``geopoint`` Geography column):

* **Writes** go through ``WKTElement(f"POINT({lng} {lat})", srid=4326)`` —
  note the PostGIS lon-lat axis order, so longitude comes first.
* **Reads** never surface the raw geography blob. ``get_by_slug`` projects the
  point into plain floats with ``ST_Y``/``ST_X`` over a ``cast(..., Geometry)``
  (geography → geometry so the accessor functions apply); both are null-safe
  and yield ``None`` when ``geopoint`` is unset.
"""

import re
from dataclasses import dataclass

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKTElement
from sehaty.db import (
    DoctorProfile,
    DoctorSpecialty,
    Specialty,
    User,
    UserRole,
    VerificationStatus,
)
from sqlalchemy import cast, delete, func, select

from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError
from sehaty.core.services import doctors as doctor_service

_MAX_LIMIT = 100
_SRID = 4326


@dataclass(frozen=True)
class SpecialtyRef:
    """One specialty a doctor is tagged with (localized names)."""

    slug: str
    name_en: str
    name_fr: str
    name_ar: str


@dataclass(frozen=True)
class DoctorView:
    """Public, read-only projection of a doctor's profile.

    The raw PostGIS ``geopoint`` is never exposed: coordinates arrive as plain
    ``lat``/``lng`` floats (or ``None``). ``languages`` has no column in the
    current schema, so it is surfaced as an empty list until one lands.
    """

    slug: str
    full_name: str
    bio: str | None
    photo_url: str | None
    address: str | None
    city: str | None
    lat: float | None
    lng: float | None
    consultation_fee: float | None
    languages: list[str]
    verification_status: str
    specialties: list[SpecialtyRef]


def _slugify(value: str) -> str:
    """lowercase, non-alnum → ``-``, collapse runs, trim edge dashes."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower())
    return slug.strip("-")


class DoctorController:
    @staticmethod
    def search(
        *,
        city: str | None = None,
        query: str | None = None,
        limit: int = 20,
    ) -> list[DoctorProfile]:
        """Validate marketplace search inputs and delegate to the service."""
        if limit <= 0 or limit > _MAX_LIMIT:
            raise SehatyValidationError(f"limit out of range: {limit}")
        if query is not None and not query.strip():
            raise SehatyValidationError("query must not be blank")

        return doctor_service.search_doctors(
            city=city.strip() if city else None,
            query=query.strip() if query else None,
            limit=limit,
        )

    @staticmethod
    def upsert_profile(
        user_id: int,
        *,
        full_name: str,
        bio: str | None = None,
        photo_url: str | None = None,
        address: str | None = None,
        city: str | None = None,
        lat: float | None = None,
        lng: float | None = None,
        consultation_fee: float | None = None,
        languages: list[str] | None = None,
        specialty_slugs: list[str] | None = None,
    ) -> str:
        """Create or update the caller's ``DoctorProfile`` and return its slug.

        The caller must be a ``DOCTOR`` user (``SehatyNotFoundError`` otherwise).

        On first create a unique slug is derived from ``full_name`` (via
        ``_slugify``, disambiguated with ``-2``, ``-3``, … when taken) and never
        changes on later updates. The profile starts ``PENDING`` — an admin
        accredits it before it becomes publicly visible via ``get_by_slug``.

        ``geopoint`` is (re)written only when both ``lat`` and ``lng`` are given.
        ``specialty_slugs`` fully replaces the doctor's specialty links; an
        unknown slug raises ``SehatyValidationError`` (fail loud rather than
        silently drop a caller's intent). ``languages`` is accepted but not yet
        persisted (no column in the current schema).
        """
        if not full_name or not full_name.strip():
            raise SehatyValidationError("full_name is required")
        full_name = full_name.strip()
        has_point = lat is not None and lng is not None

        with get_session() as session:
            role = session.execute(select(User.role).where(User.id == user_id)).scalar_one_or_none()
            if role != UserRole.DOCTOR:
                raise SehatyNotFoundError(f"no doctor for user {user_id}")

            profile = session.execute(
                select(DoctorProfile).where(DoctorProfile.user_id == user_id)
            ).scalar_one_or_none()

            if profile is None:
                slug = DoctorController._unique_slug(session, full_name)
                profile = DoctorProfile(
                    user_id=user_id,
                    full_name=full_name,
                    slug=slug,
                    # license_no is NOT NULL/unique but has no upsert input; mint
                    # a per-user placeholder for the profile-completion path
                    # (mirrors the synthetic email trick in AuthController).
                    license_no=f"PENDING-{user_id}",
                    verification_status=VerificationStatus.PENDING,
                )
                session.add(profile)
            else:
                slug = profile.slug  # slug is immutable after first create
                profile.full_name = full_name

            profile.bio = bio
            profile.photo_url = photo_url
            profile.address = address
            profile.city = city
            profile.consultation_fee = consultation_fee
            if has_point:
                profile.geopoint = WKTElement(f"POINT({lng} {lat})", srid=_SRID)

            DoctorController._replace_specialties(session, user_id, specialty_slugs or [])
            return slug

    @staticmethod
    def get_by_slug(slug: str) -> DoctorView:
        """Return the public view of a VERIFIED doctor, else ``NotFound``.

        Only VERIFIED doctors are surfaced publicly: a PENDING/REJECTED (or
        missing) slug raises ``SehatyNotFoundError`` — we don't leak the
        existence of unverified profiles. Coordinates are read via
        ``ST_Y``/``ST_X`` so the raw geography is never returned.
        """
        stmt = select(
            DoctorProfile.user_id,
            DoctorProfile.slug,
            DoctorProfile.full_name,
            DoctorProfile.bio,
            DoctorProfile.photo_url,
            DoctorProfile.address,
            DoctorProfile.city,
            func.ST_Y(cast(DoctorProfile.geopoint, Geometry)).label("lat"),
            func.ST_X(cast(DoctorProfile.geopoint, Geometry)).label("lng"),
            DoctorProfile.consultation_fee,
            DoctorProfile.verification_status,
        ).where(DoctorProfile.slug == slug)

        with get_session() as session:
            row = session.execute(stmt).one_or_none()
            if row is None or row.verification_status != VerificationStatus.VERIFIED:
                raise SehatyNotFoundError(f"no verified doctor for slug {slug!r}")

            specs = session.execute(
                select(
                    Specialty.slug,
                    Specialty.name_en,
                    Specialty.name_fr,
                    Specialty.name_ar,
                )
                .join(DoctorSpecialty, DoctorSpecialty.specialty_id == Specialty.id)
                .where(DoctorSpecialty.doctor_id == row.user_id)
                .order_by(Specialty.name_en)
            ).all()

        return DoctorView(
            slug=row.slug,
            full_name=row.full_name,
            bio=row.bio,
            photo_url=row.photo_url,
            address=row.address,
            city=row.city,
            lat=row.lat,
            lng=row.lng,
            consultation_fee=row.consultation_fee,
            languages=[],
            verification_status=str(row.verification_status),
            specialties=[
                SpecialtyRef(
                    slug=s.slug,
                    name_en=s.name_en,
                    name_fr=s.name_fr,
                    name_ar=s.name_ar,
                )
                for s in specs
            ],
        )

    @staticmethod
    def _unique_slug(session, base_name: str) -> str:
        """Slugify ``base_name`` and suffix ``-2``, ``-3``, … until unique."""
        base = _slugify(base_name) or "doctor"
        taken = set(
            session.execute(select(DoctorProfile.slug).where(DoctorProfile.slug.like(f"{base}%")))
            .scalars()
            .all()
        )
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    @staticmethod
    def _replace_specialties(session, doctor_id: int, slugs: list[str]) -> None:
        """Point the doctor's specialty links at exactly ``slugs``."""
        session.execute(delete(DoctorSpecialty).where(DoctorSpecialty.doctor_id == doctor_id))
        seen: set[str] = set()
        for slug in slugs:
            if slug in seen:
                continue
            seen.add(slug)
            specialty_id = session.execute(
                select(Specialty.id).where(Specialty.slug == slug)
            ).scalar_one_or_none()
            if specialty_id is None:
                raise SehatyValidationError(f"unknown specialty slug: {slug!r}")
            session.add(DoctorSpecialty(doctor_id=doctor_id, specialty_id=specialty_id))
