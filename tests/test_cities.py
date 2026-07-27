"""Pure-SQLite tests for the city/district browse axes and place slugs.

Covers ``place_slug`` (the URL segments the landing routes are built from),
``CityController`` and the new city/district filters on the directory. Same
dialect-scoped ``Geography`` shim as the other SQLite suites — no point is ever
written, so ``geopoint`` stays NULL and is never read.
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

from sehaty.core.controllers.cities import CityController
from sehaty.core.controllers.directory import DoctorDirectoryController
from sehaty.core.db import session as session_mod
from sehaty.core.places import match_display_names, place_slug


@compiles(Geography, "sqlite")
def _geography_as_text_on_sqlite(element, compiler, **kw) -> str:
    return "TEXT"


@compiles(geo_functions.ST_GeogFromText, "sqlite")
def _geog_bind_passthrough_on_sqlite(element, compiler, **kw) -> str:
    return compiler.process(list(element.clauses)[0], **kw)


@compiles(geo_functions.ST_AsEWKB, "sqlite")
@compiles(geo_functions.ST_AsBinary, "sqlite")
def _read_geopoint_passthrough_on_sqlite(element, compiler, **kw) -> str:
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


def _doctor(
    session: Session,
    *,
    name: str,
    city: str | None,
    district: str | None = None,
    specialty: Specialty | None = None,
    verified: bool = True,
    active: bool = True,
) -> None:
    user = User(email=f"{place_slug(name)}@clinic.ma", role=UserRole.DOCTOR, is_active=active)
    session.add(user)
    session.flush()
    session.add(
        DoctorProfile(
            user_id=user.id,
            full_name=name,
            slug=place_slug(name),
            license_no=f"LIC-{user.id}",
            city=city,
            district=district,
            verification_status=(
                VerificationStatus.VERIFIED if verified else VerificationStatus.PENDING
            ),
        )
    )
    if specialty is not None:
        session.add(DoctorSpecialty(doctor_id=user.id, specialty_id=specialty.id))


@pytest.fixture
def seeded(db: sessionmaker[Session]) -> sessionmaker[Session]:
    with db() as s:
        dentistry = Specialty(
            slug="dentistry", name_en="Dentist", name_fr="Dentiste", name_ar="أسنان"
        )
        cardio = Specialty(
            slug="cardiology", name_en="Cardiologist", name_fr="Cardiologue", name_ar="قلب"
        )
        s.add_all([dentistry, cardio])
        s.flush()

        # Casablanca stored three different ways — one browse page, not three.
        _doctor(s, name="Dr A", city="Casablanca", district="Maârif", specialty=dentistry)
        _doctor(s, name="Dr B", city="casablanca", district="Maarif", specialty=dentistry)
        _doctor(s, name="Dr C", city="CASABLANCA", district="Gauthier", specialty=cardio)
        _doctor(s, name="Dr D", city="Rabat", district="Agdal", specialty=dentistry)
        # Must never surface: unverified, inactive, or no city at all.
        _doctor(s, name="Dr E", city="Fès", specialty=dentistry, verified=False)
        _doctor(s, name="Dr F", city="Tanger", specialty=dentistry, active=False)
        _doctor(s, name="Dr G", city=None, specialty=dentistry)
        s.commit()
    return db


class TestPlaceSlug:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Casablanca", "casablanca"),
            # Accents must fold, not vanish: "ma-rif" is a URL nobody types.
            ("Maârif", "maarif"),
            ("Aïn Diab", "ain-diab"),
            ("Fès", "fes"),
            ("Sidi Maârouf", "sidi-maarouf"),
            ("  Hay   Hassani  ", "hay-hassani"),
            ("Derb Sultan / El Fida", "derb-sultan-el-fida"),
            ("CASABLANCA", "casablanca"),
        ],
    )
    def test_slugs(self, name: str, expected: str) -> None:
        assert place_slug(name) == expected

    def test_matches_every_stored_spelling(self) -> None:
        stored = ["Casablanca", "casablanca ", "CASABLANCA", "Rabat", None]
        assert match_display_names("casablanca", stored) == [
            "Casablanca",
            "casablanca ",
            "CASABLANCA",
        ]

    def test_unknown_slug_matches_nothing(self) -> None:
        assert match_display_names("atlantis", ["Casablanca"]) == []


class TestListCities:
    def test_groups_spellings_and_counts_doctors(self, seeded: sessionmaker[Session]) -> None:
        cities = {c.slug: c for c in CityController.list_cities()}
        assert set(cities) == {"casablanca", "rabat"}
        assert cities["casablanca"].doctor_count == 3
        assert cities["rabat"].doctor_count == 1

    def test_label_is_the_most_common_spelling(self, seeded: sessionmaker[Session]) -> None:
        # Each Casablanca spelling appears once, so the label must at least be a
        # real stored value rather than an invented normalization.
        label = next(c.label for c in CityController.list_cities() if c.slug == "casablanca")
        assert label in {"Casablanca", "casablanca", "CASABLANCA"}

    def test_orders_by_doctor_count(self, seeded: sessionmaker[Session]) -> None:
        assert [c.slug for c in CityController.list_cities()] == ["casablanca", "rabat"]

    def test_excludes_unverified_inactive_and_cityless(self, seeded: sessionmaker[Session]) -> None:
        slugs = {c.slug for c in CityController.list_cities()}
        # A city whose only doctor is PENDING must not become a page.
        assert "fes" not in slugs
        assert "tanger" not in slugs


class TestListDistricts:
    def test_lists_neighbourhoods_of_a_city(self, seeded: sessionmaker[Session]) -> None:
        districts = {d.slug: d.doctor_count for d in CityController.list_districts("casablanca")}
        assert districts == {"maarif": 2, "gauthier": 1}

    def test_unknown_city_yields_nothing(self, seeded: sessionmaker[Session]) -> None:
        assert CityController.list_districts("atlantis") == []


class TestListCitySpecialties:
    def test_counts_specialties_within_the_city(self, seeded: sessionmaker[Session]) -> None:
        specs = {s.slug: s.doctor_count for s in CityController.list_city_specialties("casablanca")}
        # Rabat's dentist must not inflate Casablanca's count.
        assert specs == {"dentistry": 2, "cardiology": 1}

    def test_orders_by_local_doctor_count(self, seeded: sessionmaker[Session]) -> None:
        slugs = [s.slug for s in CityController.list_city_specialties("casablanca")]
        assert slugs == ["dentistry", "cardiology"]

    def test_unknown_city_yields_nothing(self, seeded: sessionmaker[Session]) -> None:
        assert CityController.list_city_specialties("atlantis") == []


class TestDirectoryPlaceFilters:
    def test_city_filter_spans_every_spelling(self, seeded: sessionmaker[Session]) -> None:
        page = DoctorDirectoryController.list_directory(city="casablanca")
        assert page.total == 3
        assert {d.full_name for d in page.doctors} == {"Dr A", "Dr B", "Dr C"}

    def test_district_narrows_within_the_city(self, seeded: sessionmaker[Session]) -> None:
        page = DoctorDirectoryController.list_directory(city="casablanca", district="maarif")
        assert page.total == 2
        assert {d.full_name for d in page.doctors} == {"Dr A", "Dr B"}

    def test_combines_with_specialty(self, seeded: sessionmaker[Session]) -> None:
        page = DoctorDirectoryController.list_directory(city="casablanca", specialty="cardiology")
        assert page.total == 1
        assert page.doctors[0].full_name == "Dr C"

    def test_unknown_city_is_an_empty_page_not_an_error(
        self, seeded: sessionmaker[Session]
    ) -> None:
        page = DoctorDirectoryController.list_directory(city="atlantis")
        assert page.total == 0
        assert page.doctors == []

    def test_total_reflects_the_place_filter(self, seeded: sessionmaker[Session]) -> None:
        # `total` drives pagination — if the filter were applied after counting,
        # the city page would advertise doctors from every other city.
        page = DoctorDirectoryController.list_directory(city="rabat", limit=1)
        assert page.total == 1

    def test_district_is_returned_on_each_row(self, seeded: sessionmaker[Session]) -> None:
        page = DoctorDirectoryController.list_directory(city="casablanca", district="gauthier")
        assert page.doctors[0].district == "Gauthier"
