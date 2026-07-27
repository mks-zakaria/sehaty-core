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
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

from sehaty.core._dto import DomainModel
from sehaty.core.controllers.landing_config import (
    LandingConfig,
    LandingConfigController,
)
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError
from sehaty.core.services import doctors as doctor_service
from sehaty.core.services.entitlement import booking_enabled
from sehaty.core.services.slots import available_slots

_MAX_LIMIT = 100
_SRID = 4326


class SpecialtyRef(DomainModel):
    """One specialty a doctor is tagged with (localized names)."""

    slug: str
    name_en: str
    name_fr: str
    name_ar: str


class OpeningHours(DomainModel):
    """One open weekday and the wall-clock ranges the cabinet is open that day.

    ``weekday`` follows ``Availability.weekday`` (0=Monday .. 6=Sunday). A day
    the cabinet is closed is simply absent from the list. ``ranges`` holds
    ``("HH:MM", "HH:MM")`` pairs in the doctor's ``timezone`` — two of them is the
    norm here, since Moroccan cabinets almost always break at midday.
    """

    weekday: int
    ranges: list[tuple[str, str]]


def _validate_opening_hours(raw: list[dict]) -> list[dict]:
    """Normalize and validate a submitted opening-hours payload.

    Strict on write (garbage hours silently break both the public page and the
    ``openingHoursSpecification`` JSON-LD), permissive on read. Returns the
    normalized payload sorted by weekday.
    """
    seen: set[int] = set()
    normalized: list[dict] = []

    for entry in raw:
        if not isinstance(entry, dict):
            raise SehatyValidationError(f"opening_hours entry must be an object: {entry!r}")
        weekday = entry.get("weekday")
        if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
            raise SehatyValidationError(f"weekday must be an int 0-6, got {weekday!r}")
        if weekday in seen:
            raise SehatyValidationError(f"duplicate weekday in opening_hours: {weekday}")
        seen.add(weekday)

        ranges: list[list[str]] = []
        previous_end = ""
        for pair in entry.get("ranges") or []:
            if len(pair) != 2:
                raise SehatyValidationError(f"opening_hours range must be a pair: {pair!r}")
            start, end = (_validate_hhmm(value) for value in pair)
            if start >= end:
                raise SehatyValidationError(f"range start must precede end: {start}-{end}")
            # Lexical comparison is correct for zero-padded HH:MM, and rejecting
            # overlaps keeps the rendered "open now" check unambiguous.
            if previous_end and start < previous_end:
                raise SehatyValidationError(f"overlapping ranges on weekday {weekday}")
            previous_end = end
            ranges.append([start, end])

        if not ranges:
            # A weekday with no ranges means closed — drop it rather than storing
            # an empty entry, so "absent = closed" stays the single convention.
            continue
        normalized.append({"weekday": weekday, "ranges": ranges})

    return sorted(normalized, key=lambda entry: entry["weekday"])


def _validate_hhmm(value: object) -> str:
    """Accept a zero-padded 24h ``HH:MM`` string, else raise."""
    if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise SehatyValidationError(f"time must be HH:MM (24h), got {value!r}")
    return value


class DoctorView(DomainModel):
    """Public, read-only projection of a doctor's profile.

    The raw PostGIS ``geopoint`` is never exposed: coordinates arrive as plain
    ``lat``/``lng`` floats (or ``None``). ``languages`` is the doctor's spoken
    languages (``ar``/``fr``/``en``) read straight off the profile column.

    ``id`` is the doctor's ``user_id`` — the numeric handle the booking flow
    (``/dr/:slug`` → ``/book``) needs to create an appointment.
    """

    id: int
    slug: str
    full_name: str
    bio: str | None
    photo_url: str | None
    address: str | None
    city: str | None
    district: str | None
    lat: float | None
    lng: float | None
    consultation_fee: float | None
    languages: list[str]
    timezone: str
    verification_status: str
    # UNCLAIMED pages carry a "vous êtes ce médecin ?" banner and a removal link.
    claim_status: str
    # False when the subscription has lapsed. The page stays live either way —
    # only online booking is switched off.
    booking_enabled: bool
    # Public cabinet contact — the landing page's three CTAs (call / WhatsApp /
    # directions). Never the doctor's login phone, which stays private.
    phone_fixe: str | None
    phone_mobile: str | None
    whatsapp: str | None
    # Weekly opening hours, one entry per open weekday; see ``DoctorProfile``.
    opening_hours: list[OpeningHours]
    insurances: list[str]
    tiers_payant: bool
    # How this doctor's page is built: which specialty template, and their own
    # content when they are on the paid tier. Resolved server-side so the
    # landing app never has to know the specialty->template mapping.
    landing: LandingConfig
    specialties: list[SpecialtyRef]


def _slugify(value: str) -> str:
    """lowercase, non-alnum → ``-``, collapse runs, trim edge dashes."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower())
    return slug.strip("-")


class DoctorController:
    @staticmethod
    def list_published_slugs() -> list[str]:
        """Every VERIFIED, active doctor's slug — the set of public pages.

        Exists for the landing site's ``generateStaticParams``: it needs the full
        slug list in one call, unpaginated and without the per-doctor payload the
        directory carries. Same eligibility as ``get_by_slug``, so a slug
        returned here always resolves to a page rather than a 404.
        """
        stmt = (
            select(DoctorProfile.slug)
            .join(User, User.id == DoctorProfile.user_id)
            .where(
                User.is_active.is_(True),
                DoctorProfile.verification_status == VerificationStatus.VERIFIED,
            )
            .order_by(DoctorProfile.slug.asc())
        )
        with get_session() as session:
            return list(session.execute(stmt).scalars().all())

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
        district: str | None = None,
        lat: float | None = None,
        lng: float | None = None,
        consultation_fee: float | None = None,
        languages: list[str] | None = None,
        timezone: str | None = None,
        phone_fixe: str | None = None,
        phone_mobile: str | None = None,
        whatsapp: str | None = None,
        opening_hours: list[dict] | None = None,
        insurances: list[str] | None = None,
        tiers_payant: bool | None = None,
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
        silently drop a caller's intent). ``languages`` fully replaces the
        stored list when given; passing ``None`` on update leaves the existing
        list untouched (create defaults it to ``[]``).

        ``timezone`` is the clinic's IANA zone. It is validated against
        ``zoneinfo`` (an unknown zone raises ``SehatyValidationError``). On create
        it defaults to ``"Africa/Casablanca"``; on update, ``None`` leaves the
        stored zone untouched (like the other optional fields).

        ``opening_hours``, ``insurances`` and ``tiers_payant`` follow the
        ``languages`` convention: ``None`` leaves the stored value untouched, a
        value fully replaces it. ``opening_hours`` is validated and normalized
        (see ``_validate_opening_hours``); ``insurances`` is lowercased and
        de-duplicated while keeping the caller's order.
        """
        if not full_name or not full_name.strip():
            raise SehatyValidationError("full_name is required")
        full_name = full_name.strip()
        has_point = lat is not None and lng is not None
        if timezone is not None:
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise SehatyValidationError(f"unknown timezone: {timezone!r}") from exc
        if opening_hours is not None:
            opening_hours = _validate_opening_hours(opening_hours)
        if insurances is not None:
            insurances = list(dict.fromkeys(s.strip().lower() for s in insurances if s.strip()))

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
                    languages=languages if languages is not None else [],
                    timezone=timezone or "Africa/Casablanca",
                    opening_hours=opening_hours if opening_hours is not None else [],
                    insurances=insurances if insurances is not None else [],
                    tiers_payant=bool(tiers_payant),
                )
                session.add(profile)
            else:
                slug = profile.slug  # slug is immutable after first create
                profile.full_name = full_name
                # None means "leave as-is" (do not clobber the stored list);
                # a list (even empty) fully replaces it.
                if languages is not None:
                    profile.languages = languages
                # None means "leave the stored zone as-is" (like the fields above).
                if timezone is not None:
                    profile.timezone = timezone
                if opening_hours is not None:
                    profile.opening_hours = opening_hours
                if insurances is not None:
                    profile.insurances = insurances
                if tiers_payant is not None:
                    profile.tiers_payant = tiers_payant

            profile.bio = bio
            profile.photo_url = photo_url
            profile.address = address
            profile.city = city
            profile.district = district
            profile.consultation_fee = consultation_fee
            profile.phone_fixe = phone_fixe
            profile.phone_mobile = phone_mobile
            profile.whatsapp = whatsapp
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
            DoctorProfile.district,
            func.ST_Y(cast(DoctorProfile.geopoint, Geometry)).label("lat"),
            func.ST_X(cast(DoctorProfile.geopoint, Geometry)).label("lng"),
            DoctorProfile.consultation_fee,
            DoctorProfile.languages,
            DoctorProfile.timezone,
            DoctorProfile.verification_status,
            DoctorProfile.claim_status,
            DoctorProfile.phone_fixe,
            DoctorProfile.phone_mobile,
            DoctorProfile.whatsapp,
            DoctorProfile.opening_hours,
            DoctorProfile.insurances,
            DoctorProfile.tiers_payant,
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
            id=row.user_id,
            slug=row.slug,
            full_name=row.full_name,
            bio=row.bio,
            photo_url=row.photo_url,
            address=row.address,
            city=row.city,
            district=row.district,
            lat=row.lat,
            lng=row.lng,
            consultation_fee=row.consultation_fee,
            languages=list(row.languages or []),
            timezone=row.timezone,
            verification_status=str(row.verification_status),
            claim_status=str(row.claim_status),
            booking_enabled=booking_enabled(row.user_id),
            phone_fixe=row.phone_fixe,
            phone_mobile=row.phone_mobile,
            whatsapp=row.whatsapp,
            opening_hours=[
                OpeningHours(weekday=entry["weekday"], ranges=entry["ranges"])
                for entry in (row.opening_hours or [])
            ],
            insurances=list(row.insurances or []),
            tiers_payant=bool(row.tiers_payant),
            landing=LandingConfigController.for_doctor(row.user_id),
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
    def get_public_slots(slug: str, from_date: date, to_date: date) -> list[dict[str, datetime]]:
        """Return the free ``{start_at, end_at}`` slots for a VERIFIED doctor.

        Mirrors ``get_by_slug``'s privacy stance: a PENDING/REJECTED or missing
        slug raises ``SehatyNotFoundError`` — we never leak the existence of an
        unverified (or absent) profile. Only the ``user_id`` and
        ``verification_status`` columns are projected, so the PostGIS
        ``geopoint`` blob is never loaded (we don't need it, and SQLite cannot
        compile it). Slot generation reuses the same session.

        A doctor whose subscription has lapsed returns **no slots** rather than
        a 404: their page stays live and callable, only the booking engine is
        switched off. See ``sehaty.core.services.entitlement``.
        """
        stmt = select(
            DoctorProfile.user_id,
            DoctorProfile.verification_status,
        ).where(DoctorProfile.slug == slug)

        with get_session() as session:
            row = session.execute(stmt).one_or_none()
            if row is None or row.verification_status != VerificationStatus.VERIFIED:
                raise SehatyNotFoundError(f"no verified doctor for slug {slug!r}")
            if not booking_enabled(row.user_id):
                return []
            slots = available_slots(session, row.user_id, from_date, to_date)

        return [{"start_at": start, "end_at": end} for start, end in slots]

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
