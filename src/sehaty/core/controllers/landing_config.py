"""Resolving which landing template a doctor's page uses, and its content.

Resolution order, most specific first:

1. an explicit ``template`` the doctor (or an admin) chose;
2. the default for their **primary** specialty;
3. ``"general"``.

Step 2 is what makes this worth building: almost no doctor will ever pick a
template, and they should not have to. A dentist gets the dentist page because
they are a dentist.

"Primary specialty" is the first by name — a doctor tagged both `dentistry` and
`orthopedics` needs *one* page, and picking deterministically beats picking
whichever row the database happened to return first.

Content (services, equipment, FAQ, accent) is only surfaced when the doctor is
``is_personalized``. A free page still gets the right *shape* for its specialty;
what it does not get is the doctor's own words. That split is the product: the
template makes the directory good, the content is what a subscription buys.
"""

from datetime import UTC, datetime

from sehaty.db import DoctorLanding, DoctorProfile, DoctorSpecialty, Specialty
from sqlalchemy import select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError

# Specialty slug -> template key. A specialty with no entry falls to "general",
# which is correct rather than a gap: a template only earns its own key when the
# page genuinely needs different sections, not merely a different word.
SPECIALTY_TEMPLATES = {
    "dentistry": "dentistry",
    "orthopedics": "orthopedics",
    "gynecology": "gynecology",
    "psychiatry": "psychiatry",
    "pediatrics": "pediatrics",
    "dermatology": "dermatology",
    "ophthalmology": "ophthalmology",
    "cardiology": "cardiology",
    "otolaryngology": "general",
    "generalist": "general",
}

DEFAULT_TEMPLATE = "general"
KNOWN_TEMPLATES = frozenset(SPECIALTY_TEMPLATES.values()) | {DEFAULT_TEMPLATE}

_MAX_SERVICES = 40
_MAX_FAQ = 20


class ServiceItem(DomainModel):
    """One act a doctor performs, with an optional published price."""

    label: str
    price: float | None = None


class FaqItem(DomainModel):
    question: str
    answer: str


class LandingConfig(DomainModel):
    """How one doctor's public page should be built."""

    template: str
    # True when the template came from the doctor's specialty rather than an
    # explicit choice — useful in the admin console to show what is inherited.
    template_is_default: bool
    accent: str | None
    section_order: list[str]
    services: list[ServiceItem]
    equipment: list[str]
    faq: list[FaqItem]
    tagline: str | None
    is_personalized: bool


def _blank(template: str) -> LandingConfig:
    """A doctor with no row: the specialty template, no custom content."""
    return LandingConfig(
        template=template,
        template_is_default=True,
        accent=None,
        section_order=[],
        services=[],
        equipment=[],
        faq=[],
        tagline=None,
        is_personalized=False,
    )


def _primary_specialty(session, doctor_id: int) -> str | None:  # noqa: ANN001
    """The doctor's first specialty by name — deterministic across calls."""
    return session.execute(
        select(Specialty.slug)
        .join(DoctorSpecialty, DoctorSpecialty.specialty_id == Specialty.id)
        .where(DoctorSpecialty.doctor_id == doctor_id)
        .order_by(Specialty.name_en.asc())
        .limit(1)
    ).scalar_one_or_none()


class LandingConfigController:
    @staticmethod
    def for_doctor(doctor_id: int) -> LandingConfig:
        """Resolve the landing configuration for one doctor."""
        with get_session() as session:
            specialty = _primary_specialty(session, doctor_id)
            fallback = SPECIALTY_TEMPLATES.get(specialty or "", DEFAULT_TEMPLATE)

            row = session.get(DoctorLanding, doctor_id)
            if row is None:
                return _blank(fallback)

            chosen = row.template if row.template in KNOWN_TEMPLATES else None
            template = chosen or fallback

            if not row.is_personalized:
                # The free tier keeps the specialty shape but none of the
                # doctor's own content — otherwise the paid tier has nothing
                # left to sell, and a lapsed subscription would silently keep
                # publishing content they stopped paying for.
                return LandingConfig(
                    template=template,
                    template_is_default=chosen is None,
                    accent=None,
                    section_order=[],
                    services=[],
                    equipment=[],
                    faq=[],
                    tagline=None,
                    is_personalized=False,
                )

            return LandingConfig(
                template=template,
                template_is_default=chosen is None,
                accent=row.accent,
                section_order=list(row.section_order or []),
                services=[
                    ServiceItem(label=s.get("label", ""), price=s.get("price"))
                    for s in (row.services or [])
                    if s.get("label")
                ],
                equipment=[e for e in (row.equipment or []) if e],
                faq=[
                    FaqItem(question=f.get("q", ""), answer=f.get("a", ""))
                    for f in (row.faq or [])
                    if f.get("q") and f.get("a")
                ],
                tagline=row.tagline,
                is_personalized=True,
            )

    @staticmethod
    def upsert(
        doctor_id: int,
        *,
        template: str | None = None,
        accent: str | None = None,
        section_order: list[str] | None = None,
        services: list[dict] | None = None,
        equipment: list[str] | None = None,
        faq: list[dict] | None = None,
        tagline: str | None = None,
    ) -> LandingConfig:
        """Create or update a doctor's landing configuration.

        Does **not** flip ``is_personalized`` — that is a commercial decision
        made when the pack is sold, not something writing content grants. A
        doctor can fill their services in during the trial and have them go live
        the moment the sale is recorded.
        """
        if template is not None and template not in KNOWN_TEMPLATES:
            raise SehatyValidationError(f"unknown template: {template!r}")
        if accent is not None and not _is_hex_colour(accent):
            raise SehatyValidationError(f"accent must be a hex colour, got {accent!r}")
        if services is not None and len(services) > _MAX_SERVICES:
            raise SehatyValidationError(f"at most {_MAX_SERVICES} services")
        if faq is not None and len(faq) > _MAX_FAQ:
            raise SehatyValidationError(f"at most {_MAX_FAQ} FAQ entries")

        with get_session() as session:
            if session.get(DoctorProfile, doctor_id) is None:
                raise SehatyNotFoundError(f"no doctor profile for user {doctor_id}")

            row = session.get(DoctorLanding, doctor_id)
            if row is None:
                row = DoctorLanding(doctor_id=doctor_id)
                session.add(row)

            # None means "leave as-is"; a value replaces. Same convention as the
            # profile upsert, so a partial save never wipes the rest.
            if template is not None:
                row.template = template
            if accent is not None:
                row.accent = accent
            if section_order is not None:
                row.section_order = section_order
            if services is not None:
                row.services = services
            if equipment is not None:
                row.equipment = equipment
            if faq is not None:
                row.faq = faq
            if tagline is not None:
                row.tagline = tagline.strip() or None
            session.flush()

        return LandingConfigController.for_doctor(doctor_id)

    @staticmethod
    def set_personalized(doctor_id: int, *, enabled: bool) -> LandingConfig:
        """Turn the paid personalisation on or off.

        Switching it off leaves the stored content untouched: a doctor who
        stops paying and later resumes should get their services back rather
        than retype them.
        """
        with get_session() as session:
            row = session.get(DoctorLanding, doctor_id)
            if row is None:
                if session.get(DoctorProfile, doctor_id) is None:
                    raise SehatyNotFoundError(f"no doctor profile for user {doctor_id}")
                row = DoctorLanding(doctor_id=doctor_id)
                session.add(row)
            row.is_personalized = enabled
            row.personalized_at = datetime.now(UTC) if enabled else None
            session.flush()

        return LandingConfigController.for_doctor(doctor_id)


def _is_hex_colour(value: str) -> bool:
    """`#rgb`, `#rrggbb` or `#rrggbbaa`."""
    if not value.startswith("#"):
        return False
    body = value[1:]
    return len(body) in (3, 6, 8) and all(c in "0123456789abcdefABCDEF" for c in body)
