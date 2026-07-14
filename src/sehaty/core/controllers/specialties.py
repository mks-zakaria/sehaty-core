"""Medical-specialties catalogue business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern), each
method owning one transaction via ``get_session()``. The ``specialties`` table
carries no geo column, so reads can load whole entities freely — no PostGIS
shim is needed (unlike ``DoctorProfile``).

The catalogue is a small, mostly-static lookup: ``list_specialties`` exposes it
ordered by English name, and ``seed_defaults`` idempotently plants the built-in
Moroccan specialties (each localized in EN / FR / AR).
"""

from dataclasses import dataclass

from sehaty.db import Specialty
from sqlalchemy import select

from sehaty.core.db.session import get_session


@dataclass(frozen=True)
class SpecialtyView:
    """A read-only projection of one catalogue row."""

    id: int
    slug: str
    name_en: str
    name_fr: str
    name_ar: str


# (slug, name_en, name_fr, name_ar) — the built-in catalogue seeded by
# ``seed_defaults``. Kept as a module constant so tests can assert its size.
_DEFAULT_SPECIALTIES: tuple[tuple[str, str, str, str], ...] = (
    ("generalist", "General practitioner", "Médecin généraliste", "طبيب عام"),
    ("cardiology", "Cardiologist", "Cardiologue", "طبيب قلب"),
    ("gastroenterology", "Gastroenterologist", "Gastro-entérologue", "طبيب جهاز هضمي"),
    ("dermatology", "Dermatologist", "Dermatologue", "طبيب جلدية"),
    ("pediatrics", "Pediatrician", "Pédiatre", "طبيب أطفال"),
    ("dentistry", "Dentist", "Dentiste", "طبيب أسنان"),
    ("gynecology", "Gynecologist", "Gynécologue", "طبيب نساء وتوليد"),
    ("ophthalmology", "Ophthalmologist", "Ophtalmologue", "طبيب عيون"),
    ("optician", "Optician", "Opticien", "نظاراتي"),
    ("otolaryngology", "ENT", "Oto-rhino-laryngologiste", "طبيب أنف وأذن وحنجرة"),
    ("psychiatry", "Psychiatrist", "Psychiatre", "طبيب نفسي"),
    ("orthopedics", "Orthopedist", "Orthopédiste", "طبيب عظام"),
    ("neurology", "Neurologist", "Neurologue", "طبيب أعصاب"),
    ("urology", "Urologist", "Urologue", "طبيب مسالك بولية"),
    ("endocrinology", "Endocrinologist", "Endocrinologue", "طبيب غدد صماء"),
    ("pulmonology", "Pulmonologist", "Pneumologue", "طبيب رئة"),
    ("rheumatology", "Rheumatologist", "Rhumatologue", "طبيب روماتيزم"),
    ("general_surgery", "General surgeon", "Chirurgien généraliste", "جراح عام"),
    ("radiology", "Radiologist", "Radiologue", "طبيب أشعة"),
    ("nephrology", "Nephrologist", "Néphrologue", "طبيب كلى"),
)


class SpecialtyController:
    @staticmethod
    def list_specialties() -> list[SpecialtyView]:
        """Return every catalogue row as a frozen view, ordered by ``name_en``."""
        stmt = select(Specialty).order_by(Specialty.name_en)
        with get_session() as session:
            rows = session.execute(stmt).scalars().all()
        return [
            SpecialtyView(
                id=row.id,
                slug=row.slug,
                name_en=row.name_en,
                name_fr=row.name_fr,
                name_ar=row.name_ar,
            )
            for row in rows
        ]

    @staticmethod
    def seed_defaults() -> int:
        """Idempotently insert the built-in catalogue.

        Slugs already present are skipped (the ``slug`` column is unique), so
        the method is safe to run repeatedly. Returns the number of rows
        actually inserted on this call.
        """
        with get_session() as session:
            existing = set(session.execute(select(Specialty.slug)).scalars().all())
            inserted = 0
            for slug, name_en, name_fr, name_ar in _DEFAULT_SPECIALTIES:
                if slug in existing:
                    continue
                session.add(
                    Specialty(
                        slug=slug,
                        name_en=name_en,
                        name_fr=name_fr,
                        name_ar=name_ar,
                    )
                )
                inserted += 1
            return inserted
