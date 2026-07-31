"""Onboarding a doctor at the cabinet, in one pass.

The visit has a shape the console did not: you are standing in front of someone
who is either already on the platform or not, and you find out which by typing
their name. Everything after that is the same work — specialty, contact, hours,
tariff, a pin, a login — so the split between "find the imported page" and
"create a new doctor" belongs at the start of one flow rather than between two
screens.

Search comes first for a reason beyond convenience. Five thousand pages were
published from public directories, so most doctors are *already here*, and
creating a second profile for someone who has one splits their reviews, orphans
whatever QR code exists, and puts two of them in the directory. The search is
deliberately loose — accent-folded, partial, ignoring the "Dr" everyone types —
because a near miss that returns nothing is what makes an operator give up and
click "create".

Loose has its own cost, which is why the city filter exists: "Bennani" is a
common surname, and a dozen rows that read alike are as likely to end in a wrong
pick as in no pick at all. The city is the one fact the operator can always check
against the door they just walked through.
"""

from __future__ import annotations

import re
import unicodedata

from sehaty.db import (
    ClaimStatus,
    DoctorProfile,
    DoctorSpecialty,
    ProfileSource,
    Specialty,
    User,
    UserRole,
    VerificationStatus,
)
from sqlalchemy import func, or_, select

from sehaty.core._dto import DomainModel
from sehaty.core.controllers.cities import PlaceRef, group_by_slug
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyConflictError, SehatyValidationError
from sehaty.core.places import match_display_names, place_slug

# Languages a page can render. A presentation is written per language rather
# than translated, so these are the keys of bio_i18n.
LOCALES = ("fr", "ar", "ary")


class DoctorMatch(DomainModel):
    """A candidate the operator might be standing in front of."""

    doctor_id: int
    slug: str
    full_name: str
    specialty: str | None
    city: str | None
    district: str | None
    address: str | None
    phone: str | None
    claim_status: str
    verification_status: str
    # True when nobody has spoken to them yet — the usual case, and the one
    # where the operator should claim the existing page rather than make a new.
    is_unclaimed: bool


def _fold(text: str) -> str:
    """Lowercase, strip accents, drop the honorific and the punctuation.

    "Dr. Amina Bennani" and "amina bennani" have to be the same query, or the
    operator searches, sees nothing, and creates a duplicate of a doctor who is
    already in the directory.
    """
    decomposed = unicodedata.normalize("NFKD", text.strip().lower())
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    without_title = re.sub(r"^(dr|dr\.|docteur|doctor|pr|pr\.)\s+", "", ascii_only)
    return re.sub(r"[^a-z0-9؀-ۿ ]+", " ", without_title).strip()


def _findable():
    """The set of doctors onboarding is allowed to surface.

    A removal request is a tombstone, so those pages are neither findable nor
    counted in the city filter. Everything else is fair game — including the
    PENDING and claimed ones the public directory hides, because the operator is
    standing in front of the doctor and needs to see whatever page exists.
    """
    return DoctorProfile.claim_status != ClaimStatus.REMOVAL_REQUESTED


class OnboardingController:
    @staticmethod
    def search(query: str, *, city: str | None = None, limit: int = 12) -> list[DoctorMatch]:
        """Find doctors whose name looks like what was typed.

        Every word has to appear somewhere in the name, in any order — "bennani
        amina" finds "Dr Amina Bennani". Ordered unclaimed-first, because an
        unclaimed page is the one the operator is most likely there to take over.

        ``city`` narrows to one place, given as a slug or as a display name.
        Bennani is a common surname and the directory holds several of them: with
        a dozen identical-looking rows the operator either picks the wrong page
        or gives up and creates a duplicate, and the city is the one thing they
        can always read off the door they just walked through.
        """
        folded = _fold(query)
        if len(folded) < 2:
            raise SehatyValidationError("type at least two characters")
        city_slug = place_slug(city) if city else ""

        primary_specialty = (
            select(
                DoctorSpecialty.doctor_id.label("doctor_id"),
                func.min(Specialty.name_fr).label("name_fr"),
            )
            .join(Specialty, Specialty.id == DoctorSpecialty.specialty_id)
            .group_by(DoctorSpecialty.doctor_id)
            .subquery()
        )

        stmt = (
            select(
                DoctorProfile.user_id,
                DoctorProfile.slug,
                DoctorProfile.full_name,
                DoctorProfile.city,
                DoctorProfile.district,
                DoctorProfile.address,
                DoctorProfile.phone_mobile,
                DoctorProfile.phone_fixe,
                DoctorProfile.claim_status,
                DoctorProfile.verification_status,
                primary_specialty.c.name_fr.label("specialty"),
            )
            .outerjoin(primary_specialty, primary_specialty.c.doctor_id == DoctorProfile.user_id)
            .where(_findable())
        )
        for word in folded.split():
            # unaccent() is not available everywhere, so fold in Python and match
            # loosely here; the name column already holds accented text, and ILIKE
            # on each word is close enough for a dozen candidates.
            stmt = stmt.where(
                or_(
                    DoctorProfile.full_name.ilike(f"%{word}%"),
                    DoctorProfile.slug.ilike(f"%{word}%"),
                )
            )
        with get_session() as session:
            if city_slug:
                # Cities are stored as free display text, so the filter is
                # resolved through every spelling that slugifies the same way —
                # "Casablanca", "casablanca " and "CASABLANCA" are one place, and
                # an accent-folded slug ("ain-diab") never ILIKEs the stored
                # "Aïn Diab" on its own.
                spellings = match_display_names(
                    city_slug,
                    list(
                        session.execute(
                            select(DoctorProfile.city).where(_findable()).distinct()
                        ).scalars()
                    ),
                )
                if not spellings:
                    # Nobody is filed under that city. Return nothing rather than
                    # quietly widening back to the whole country: a filter that
                    # silently stops applying is how the operator ends up reading
                    # a Rabat row as the Casablanca doctor in front of them.
                    return []
                stmt = stmt.where(DoctorProfile.city.in_(spellings))

            rows = session.execute(
                stmt.order_by(
                    # Unclaimed first: that is the page the visit is usually about.
                    (DoctorProfile.claim_status != ClaimStatus.UNCLAIMED),
                    DoctorProfile.full_name.asc(),
                ).limit(limit)
            ).all()

        return [
            DoctorMatch(
                doctor_id=r.user_id,
                slug=r.slug,
                full_name=r.full_name,
                specialty=r.specialty,
                city=r.city,
                district=r.district,
                address=r.address,
                phone=r.phone_mobile or r.phone_fixe,
                claim_status=str(r.claim_status),
                verification_status=str(r.verification_status),
                is_unclaimed=r.claim_status == ClaimStatus.UNCLAIMED,
            )
            for r in rows
        ]

    @staticmethod
    def cities() -> list[PlaceRef]:
        """Cities the filter can offer, busiest first.

        Read off the searchable doctors rather than a fixed list of Moroccan
        cities, so every option has at least one page behind it: a dropdown entry
        that always returns nothing reads as a broken search, and the operator's
        next move is to create the duplicate this whole flow exists to prevent.

        Wider than the public ``/api/v1/cities`` on purpose — that one counts only
        publicly visible doctors, and onboarding has to reach the PENDING page
        too.
        """
        with get_session() as session:
            names = list(session.execute(select(DoctorProfile.city).where(_findable())).scalars())
        return group_by_slug(names)

    @staticmethod
    def create(
        *,
        full_name: str,
        city: str,
        specialty_slug: str,
        district: str | None = None,
        address: str | None = None,
    ) -> DoctorMatch:
        """Add a doctor the directory never had.

        Only for someone the search genuinely could not find. The page starts
        LISTED like an imported one — an operator typing a name has not checked
        a licence either, and the badge means exactly that.
        """
        full_name = (full_name or "").strip()
        city = (city or "").strip()
        if len(full_name) < 3:
            raise SehatyValidationError("a full name is required")
        if not city:
            raise SehatyValidationError("a city is required")

        with get_session() as session:
            specialty = session.execute(
                select(Specialty).where(Specialty.slug == specialty_slug)
            ).scalar_one_or_none()
            if specialty is None:
                raise SehatyValidationError(f"unknown specialty {specialty_slug!r}")

            base = "-".join(p for p in (place_slug(full_name), place_slug(city)) if p)
            taken = session.execute(
                select(DoctorProfile.user_id).where(DoctorProfile.slug == base)
            ).scalar_one_or_none()
            if taken:
                # Refuse rather than mint `-2`. A duplicate name in one city is
                # far more often the same person than two of them, and the whole
                # point of the search is to land on the existing page.
                raise SehatyConflictError(
                    f"{full_name} already exists in {city} — open that page instead"
                )

            user = User(
                email=f"{base}@import.invalid",
                role=UserRole.DOCTOR,
                is_active=True,
                password_hash="",
            )
            session.add(user)
            session.flush()

            profile = DoctorProfile(
                user_id=user.id,
                full_name=full_name,
                slug=base,
                license_no=f"MANUAL-{user.id}",
                city=city,
                district=(district or "").strip() or None,
                address=(address or "").strip() or None,
                claim_status=ClaimStatus.UNCLAIMED,
                source=ProfileSource.MANUAL,
                verification_status=VerificationStatus.LISTED,
            )
            session.add(profile)
            session.add(DoctorSpecialty(doctor_id=user.id, specialty_id=specialty.id))
            session.flush()

            return DoctorMatch(
                doctor_id=user.id,
                slug=base,
                full_name=full_name,
                specialty=specialty.name_fr,
                city=city,
                district=profile.district,
                address=profile.address,
                phone=None,
                claim_status=str(ClaimStatus.UNCLAIMED),
                verification_status=str(VerificationStatus.LISTED),
                is_unclaimed=True,
            )


def clean_bio_i18n(value: dict | None) -> dict:
    """Keep only the languages the page renders, and drop the blanks.

    An empty string is not a translation. Storing one makes the page render an
    empty presentation instead of falling back to the language that was written.
    """
    if not value:
        return {}
    return {
        locale: text.strip()
        for locale, text in value.items()
        if locale in LOCALES and isinstance(text, str) and text.strip()
    }
