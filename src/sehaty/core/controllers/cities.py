"""City / district browse axes for the public landing site.

Feeds the ``/{city}`` and ``/{city}/{specialty}`` pages: which cities have
doctors, which specialties exist inside a city, and which neighbourhoods. The
landing site calls these at build time to enumerate the pages worth generating,
so a city with no verified doctors never becomes a URL.

Eligibility matches the directory exactly — only VERIFIED, active doctors count.
A city whose only doctor is PENDING must not appear, or the sales pitch points at
an empty page.

Place names are grouped by slug rather than by raw value: doctors type
"Casablanca", "casablanca " and "CASABLANCA", and those are one browse page with
one count, not three. The display label is the most common spelling, so the page
heading reads the way people actually write it.
"""

from collections import Counter

from sehaty.db import (
    DoctorProfile,
    DoctorSpecialty,
    Specialty,
    User,
    VerificationStatus,
)
from sqlalchemy import func, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.places import match_display_names, place_slug


class PlaceRef(DomainModel):
    """A browsable place: the URL segment, what to print, and how many doctors."""

    slug: str
    label: str
    doctor_count: int


class CitySpecialtyRef(DomainModel):
    """A specialty available in a given city, with its local doctor count."""

    slug: str
    name_en: str
    name_fr: str
    name_ar: str
    name_ary: str | None
    doctor_count: int


def _eligible():
    """The shared VERIFIED-and-active filter, as a joined select fragment."""
    return (
        select(DoctorProfile.user_id, DoctorProfile.city, DoctorProfile.district)
        .join(User, User.id == DoctorProfile.user_id)
        .where(
            User.is_active.is_(True),
            DoctorProfile.verification_status == VerificationStatus.VERIFIED,
        )
    )


def _group_by_slug(names: list[str | None]) -> list[PlaceRef]:
    """Collapse display spellings into one entry per slug, most common wins."""
    by_slug: dict[str, Counter] = {}
    for name in names:
        if not name or not name.strip():
            continue
        by_slug.setdefault(place_slug(name), Counter())[name.strip()] += 1

    places = [
        PlaceRef(
            slug=slug,
            label=spellings.most_common(1)[0][0],
            doctor_count=sum(spellings.values()),
        )
        for slug, spellings in by_slug.items()
        if slug
    ]
    # Biggest city first — that ordering is what a "browse by city" page wants.
    return sorted(places, key=lambda p: (-p.doctor_count, p.label))


class CityController:
    @staticmethod
    def list_cities() -> list[PlaceRef]:
        """Every city with at least one VERIFIED, active doctor."""
        with get_session() as session:
            rows = session.execute(_eligible()).all()
        return _group_by_slug([row.city for row in rows])

    @staticmethod
    def list_districts(city: str) -> list[PlaceRef]:
        """Neighbourhoods inside ``city`` (a slug) that have doctors.

        Empty when the city is unknown or no doctor there recorded a district —
        the caller renders a city page without the neighbourhood breakdown
        rather than a page of dead links.
        """
        with get_session() as session:
            rows = session.execute(_eligible()).all()
        matches = set(match_display_names(city, [row.city for row in rows]))
        if not matches:
            return []
        return _group_by_slug([row.district for row in rows if row.city in matches])

    @staticmethod
    def list_city_specialties(city: str) -> list[CitySpecialtyRef]:
        """Specialties practised in ``city`` (a slug), most-served first.

        This is the list the ``/{city}`` hub links to, so each entry is a page
        that is guaranteed to have at least one doctor on it.
        """
        with get_session() as session:
            city_rows = session.execute(_eligible()).all()
            matches = set(match_display_names(city, [row.city for row in city_rows]))
            if not matches:
                return []
            doctor_ids = [row.user_id for row in city_rows if row.city in matches]
            if not doctor_ids:
                return []

            rows = session.execute(
                select(
                    Specialty.slug,
                    Specialty.name_en,
                    Specialty.name_fr,
                    Specialty.name_ar,
                    Specialty.name_ary,
                    func.count(DoctorSpecialty.doctor_id).label("doctor_count"),
                )
                .join(DoctorSpecialty, DoctorSpecialty.specialty_id == Specialty.id)
                .where(DoctorSpecialty.doctor_id.in_(doctor_ids))
                .group_by(
                    Specialty.slug,
                    Specialty.name_en,
                    Specialty.name_fr,
                    Specialty.name_ar,
                    Specialty.name_ary,
                )
                .order_by(func.count(DoctorSpecialty.doctor_id).desc(), Specialty.name_fr.asc())
            ).all()

        return [
            CitySpecialtyRef(
                slug=row.slug,
                name_en=row.name_en,
                name_fr=row.name_fr,
                name_ar=row.name_ar,
                name_ary=row.name_ary,
                doctor_count=int(row.doctor_count),
            )
            for row in rows
        ]
