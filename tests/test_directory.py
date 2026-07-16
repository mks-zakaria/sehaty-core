"""Doctor-directory (browse by specialty + rating, no geo) tests on SQLite.

The directory read is deliberately geolocation-free, so — unlike the PostGIS
``test_doctor_search`` suite — it runs entirely on an in-memory SQLite engine.
``DoctorProfile`` still carries the PostGIS ``Geography`` ``geopoint`` column,
which stock SQLite cannot build, so this module registers dialect-scoped
compilation shims (geo type -> ``TEXT``; ``ST_GeogFromText`` -> pass-through)
purely for the test engine. The controller uses a column-only projection, so
``geopoint`` itself is never selected.
"""

import pytest
from geoalchemy2 import Geography
from geoalchemy2 import functions as geo_functions
from sehaty.db import (
    DoctorProfile,
    DoctorSpecialty,
    ReputationScore,
    Specialty,
    User,
    UserRole,
    VerificationStatus,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.directory import DoctorDirectoryController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyValidationError


@compiles(Geography, "sqlite")
def _geography_as_text_on_sqlite(element, compiler, **kw) -> str:  # noqa: ANN001
    # SQLite has no geography type; store the column as opaque TEXT for tests.
    return "TEXT"


@compiles(geo_functions.ST_GeogFromText, "sqlite")
def _geog_bind_passthrough_on_sqlite(element, compiler, **kw) -> str:  # noqa: ANN001
    # Skip the PostGIS constructor SQLite lacks; bind the raw value instead.
    return compiler.process(list(element.clauses)[0], **kw)


_TABLES = [
    User.__table__,
    DoctorProfile.__table__,
    Specialty.__table__,
    DoctorSpecialty.__table__,
    ReputationScore.__table__,
]


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _seed_specialty(session: Session, slug: str, name_fr: str) -> int:
    spec = Specialty(slug=slug, name_en=slug, name_fr=name_fr, name_ar=slug)
    session.add(spec)
    session.commit()
    return spec.id


def _seed_doctor(
    session: Session,
    *,
    email: str,
    slug: str,
    full_name: str,
    specialty_ids: list[int],
    status: VerificationStatus = VerificationStatus.VERIFIED,
    avg_stars: float | None = None,
    review_count: int = 0,
    consultation_fee: float | None = 250.0,
    is_active: bool = True,
) -> int:
    user = User(email=email, role=UserRole.DOCTOR, is_active=is_active)
    session.add(user)
    session.commit()

    session.add(
        DoctorProfile(
            user_id=user.id,
            full_name=full_name,
            slug=slug,
            license_no=f"LIC-{user.id}",
            city="Casablanca",
            consultation_fee=consultation_fee,
            verification_status=status,
        )
    )
    for sid in specialty_ids:
        session.add(DoctorSpecialty(doctor_id=user.id, specialty_id=sid))
    if avg_stars is not None:
        session.add(
            ReputationScore(user_id=user.id, avg_stars=avg_stars, review_count=review_count)
        )
    session.commit()
    return user.id


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> dict[str, int]:
    """Three VERIFIED doctors across two specialties + one PENDING doctor.

    ``dr-alpha`` (cardiology, 5.0/100), ``dr-beta`` (cardiology+dermatology,
    3.0/50), ``dr-gamma`` (dermatology, no ReputationScore -> 0/0) and an
    excluded ``dr-pending`` cardiologist.
    """
    with db() as s:
        cardiology = _seed_specialty(s, "cardiology", "Cardiologue")
        dermatology = _seed_specialty(s, "dermatology", "Dermatologue")

        _seed_doctor(
            s,
            email="alpha@clinic.ma",
            slug="dr-alpha",
            full_name="Alpha Bennani",
            specialty_ids=[cardiology],
            avg_stars=5.0,
            review_count=100,
        )
        _seed_doctor(
            s,
            email="beta@clinic.ma",
            slug="dr-beta",
            full_name="Beta Cherkaoui",
            specialty_ids=[cardiology, dermatology],
            avg_stars=3.0,
            review_count=50,
        )
        _seed_doctor(
            s,
            email="gamma@clinic.ma",
            slug="dr-gamma",
            full_name="Gamma Alaoui",
            specialty_ids=[dermatology],
            # No ReputationScore at all -> avg_stars/review_count coalesce to 0.
        )
        _seed_doctor(
            s,
            email="pending@clinic.ma",
            slug="dr-pending",
            full_name="Zed Pending",
            specialty_ids=[cardiology],
            status=VerificationStatus.PENDING,
            avg_stars=5.0,
            review_count=999,
        )
    return {"cardiology": cardiology, "dermatology": dermatology}


def test_lists_only_verified_rating_sorted(seeded: dict[str, int]) -> None:
    page = DoctorDirectoryController.list_directory()

    # Only the three VERIFIED doctors; the PENDING one never surfaces.
    assert page.total == 3
    assert [d.slug for d in page.doctors] == ["dr-alpha", "dr-beta", "dr-gamma"]
    # Rating desc, review-less doctor (gamma, 0) sorts last.
    assert [d.avg_stars for d in page.doctors] == [5.0, 3.0, 0.0]


def test_row_carries_specialties_and_reputation(seeded: dict[str, int]) -> None:
    by_slug = {d.slug: d for d in DoctorDirectoryController.list_directory().doctors}

    # Multi-specialty doctor: both names, alphabetical (name_fr).
    assert by_slug["dr-beta"].specialties == ["Cardiologue", "Dermatologue"]
    assert by_slug["dr-alpha"].specialties == ["Cardiologue"]

    # Reputation coalesces to 0 when no ReputationScore row exists.
    gamma = by_slug["dr-gamma"]
    assert gamma.avg_stars == 0.0
    assert gamma.review_count == 0
    assert gamma.specialties == ["Dermatologue"]
    # Column-only fields round-trip.
    assert gamma.city == "Casablanca"
    assert by_slug["dr-alpha"].consultation_fee == 250.0
    assert by_slug["dr-alpha"].review_count == 100


def test_specialty_filter_narrows(seeded: dict[str, int]) -> None:
    cardio = DoctorDirectoryController.list_directory(specialty="cardiology")
    assert cardio.total == 2
    assert {d.slug for d in cardio.doctors} == {"dr-alpha", "dr-beta"}

    derm = DoctorDirectoryController.list_directory(specialty="dermatology")
    assert derm.total == 2
    assert {d.slug for d in derm.doctors} == {"dr-beta", "dr-gamma"}


def test_sort_by_name(seeded: dict[str, int]) -> None:
    page = DoctorDirectoryController.list_directory(sort="name")
    # full_name asc: Alpha, Beta, Gamma.
    assert [d.slug for d in page.doctors] == ["dr-alpha", "dr-beta", "dr-gamma"]


def test_sort_by_reviews(seeded: dict[str, int]) -> None:
    page = DoctorDirectoryController.list_directory(sort="reviews")
    assert [d.review_count for d in page.doctors] == [100, 50, 0]
    assert [d.slug for d in page.doctors] == ["dr-alpha", "dr-beta", "dr-gamma"]


def test_pagination(seeded: dict[str, int]) -> None:
    first = DoctorDirectoryController.list_directory(limit=1, offset=0)
    assert first.total == 3
    assert [d.slug for d in first.doctors] == ["dr-alpha"]

    second = DoctorDirectoryController.list_directory(limit=1, offset=1)
    assert second.total == 3
    assert [d.slug for d in second.doctors] == ["dr-beta"]

    tail = DoctorDirectoryController.list_directory(limit=10, offset=2)
    assert tail.total == 3
    assert [d.slug for d in tail.doctors] == ["dr-gamma"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sort": "closest"},
        {"limit": 0},
        {"limit": 1000},
        {"offset": -1},
    ],
)
def test_invalid_inputs_raise(seeded: dict[str, int], kwargs: dict) -> None:
    with pytest.raises(SehatyValidationError):
        DoctorDirectoryController.list_directory(**kwargs)
