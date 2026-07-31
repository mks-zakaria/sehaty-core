"""Finding a doctor at the cabinet before creating a second one for them.

Five thousand pages were published from public directories, so most doctors are
already here. A search that misses is not a small annoyance: the operator clicks
"create", and that doctor now has two pages, split reviews, and a QR code
pointing at the abandoned one.

So these tests are mostly about the ways a name is typed wrong.
"""

import pytest
from sehaty.db import ClaimStatus, DoctorProfile, DoctorSpecialty, Specialty, User, UserRole
from sqlalchemy import update
from sqlalchemy.orm import Session

from sehaty.core.controllers.onboarding import OnboardingController, clean_bio_i18n
from sehaty.core.errors import SehatyConflictError, SehatyValidationError


def _specialty(session: Session, slug: str = "dentistry") -> int:
    existing = session.query(Specialty).filter_by(slug=slug).first()
    if existing:
        return existing.id
    s = Specialty(slug=slug, name_en=slug, name_fr="Dentiste", name_ar=slug)
    session.add(s)
    session.commit()
    return s.id


def _doctor(
    session: Session,
    name: str,
    slug: str,
    claim=ClaimStatus.UNCLAIMED,
    city: str = "Casablanca",
) -> int:
    user = User(email=f"{slug}@import.invalid", role=UserRole.DOCTOR, is_active=True)
    session.add(user)
    session.commit()
    session.add(
        DoctorProfile(
            user_id=user.id,
            full_name=name,
            slug=slug,
            license_no=f"LIC-{user.id}",
            city=city,
            claim_status=claim,
        )
    )
    session.add(DoctorSpecialty(doctor_id=user.id, specialty_id=_specialty(session)))
    session.commit()
    return int(user.id)


@pytest.mark.usefixtures("_pg_engine")
class TestOnboardingSearch:
    def test_finds_a_doctor_however_the_name_is_typed(self, pg_session: Session) -> None:
        """Each of these is what an operator actually types at a desk."""
        _doctor(pg_session, "Dr Amina Bennani", "dr-amina-bennani-casablanca")

        for query in (
            "amina bennani",
            "Bennani",
            "bennani amina",  # surname first, as directories print it
            "Dr. Amina Bennani",  # the honorific everyone includes
            "AMINA BENNANI",
        ):
            found = OnboardingController.search(query)
            assert [m.full_name for m in found] == ["Dr Amina Bennani"], query

    def test_an_accent_does_not_hide_a_doctor(self, pg_session: Session) -> None:
        """Nobody types the circumflex, and a miss here creates a duplicate."""
        _doctor(pg_session, "Dr Hasnaâ Faradi", "dr-hasnaa-faradi-casablanca")

        assert len(OnboardingController.search("hasnaa faradi")) == 1

    def test_unclaimed_pages_come_first(self, pg_session: Session) -> None:
        """The unclaimed page is the one the visit is usually about."""
        _doctor(pg_session, "Dr Karim Alami", "dr-karim-alami-casablanca", ClaimStatus.CLAIMED)
        _doctor(pg_session, "Dr Karim Alaoui", "dr-karim-alaoui-casablanca")

        assert OnboardingController.search("karim")[0].is_unclaimed is True

    def test_a_delisted_doctor_never_resurfaces(self, pg_session: Session) -> None:
        """A removal is a tombstone; onboarding must not walk them back in."""
        uid = _doctor(pg_session, "Dr Gone Away", "dr-gone-away-casablanca")
        with pg_session.begin():
            pg_session.execute(
                update(DoctorProfile)
                .where(DoctorProfile.user_id == uid)
                .values(claim_status=ClaimStatus.REMOVAL_REQUESTED)
            )

        assert OnboardingController.search("gone away") == []

    def test_a_single_character_is_refused(self, pg_session: Session) -> None:
        with pytest.raises(SehatyValidationError):
            OnboardingController.search("a")


@pytest.mark.usefixtures("_pg_engine")
class TestOnboardingCityFilter:
    """Telling two doctors with the same surname apart.

    Picking the wrong Bennani is as expensive as creating a duplicate: the hours,
    the pin and the login all land on a cabinet in another city.
    """

    def test_narrows_to_the_city_in_front_of_the_operator(self, pg_session: Session) -> None:
        casa = _doctor(pg_session, "Dr Amina Bennani", "dr-amina-bennani-casablanca")
        _doctor(pg_session, "Dr Amina Bennani", "dr-amina-bennani-rabat", city="Rabat")

        assert len(OnboardingController.search("bennani")) == 2
        assert [m.doctor_id for m in OnboardingController.search("bennani", city="casablanca")] == [
            casa
        ]

    def test_a_spelling_or_an_accent_does_not_empty_the_filter(self, pg_session: Session) -> None:
        """The import left three spellings of one city, and nobody types the accent."""
        _doctor(pg_session, "Dr Karim Alami", "dr-karim-alami-sale", city="Salé")
        _doctor(pg_session, "Dr Nadia Alami", "dr-nadia-alami-sale", city="sale ")

        assert len(OnboardingController.search("alami", city="sale")) == 2
        # A display name works as well as a slug — the console sends either.
        assert len(OnboardingController.search("alami", city="Salé")) == 2

    def test_an_unknown_city_returns_nothing_rather_than_everything(
        self, pg_session: Session
    ) -> None:
        """A filter that silently stops applying is how the wrong page gets opened."""
        _doctor(pg_session, "Dr Amina Bennani", "dr-amina-bennani-casablanca")

        assert OnboardingController.search("bennani", city="tanger") == []

    def test_cities_offers_every_place_with_a_findable_doctor(self, pg_session: Session) -> None:
        """Busiest first, one entry per city however it was spelled."""
        _doctor(pg_session, "Dr One", "dr-one-casa", city="Casablanca")
        _doctor(pg_session, "Dr Two", "dr-two-casa", city="casablanca")
        _doctor(pg_session, "Dr Three", "dr-three-rabat", city="Rabat")

        offered = OnboardingController.cities()

        assert [(p.slug, p.doctor_count) for p in offered] == [
            ("casablanca", 2),
            ("rabat", 1),
        ]
        # One label, spelled the way most doctors have it.
        assert offered[0].label == "Casablanca"

    def test_a_delisted_doctor_does_not_hold_a_city_open(self, pg_session: Session) -> None:
        """Same tombstone rule as the search, or the filter offers a dead city."""
        uid = _doctor(pg_session, "Dr Gone Away", "dr-gone-away-tanger", city="Tanger")
        with pg_session.begin():
            pg_session.execute(
                update(DoctorProfile)
                .where(DoctorProfile.user_id == uid)
                .values(claim_status=ClaimStatus.REMOVAL_REQUESTED)
            )

        assert "tanger" not in {p.slug for p in OnboardingController.cities()}


@pytest.mark.usefixtures("_pg_engine")
class TestOnboardingCreate:
    def test_creates_a_doctor_the_directory_never_had(self, pg_session: Session) -> None:
        _specialty(pg_session)

        created = OnboardingController.create(
            full_name="Dr Nouvelle Praticienne",
            city="Casablanca",
            specialty_slug="dentistry",
            district="Maârif",
        )

        assert created.is_unclaimed is True
        # LISTED, never VERIFIED: typing a name is not checking a licence.
        assert created.verification_status == "LISTED"
        assert OnboardingController.search("nouvelle praticienne")[0].doctor_id == (
            created.doctor_id
        )

    def test_refuses_to_duplicate_an_existing_doctor(self, pg_session: Session) -> None:
        """The whole point of searching first."""
        _specialty(pg_session)
        _doctor(pg_session, "Dr Amina Bennani", "dr-amina-bennani-casablanca")

        with pytest.raises(SehatyConflictError):
            OnboardingController.create(
                full_name="Dr Amina Bennani", city="Casablanca", specialty_slug="dentistry"
            )

    def test_an_unknown_specialty_is_refused(self, pg_session: Session) -> None:
        with pytest.raises(SehatyValidationError):
            OnboardingController.create(
                full_name="Dr X Y", city="Casablanca", specialty_slug="astrology"
            )


def test_blank_translations_are_dropped_not_stored() -> None:
    """An empty string is not a translation.

    Storing one makes the page render an empty presentation instead of falling
    back to the language that was actually written.
    """
    assert clean_bio_i18n({"fr": "Cabinet au Maârif.", "ar": "   ", "ary": ""}) == {
        "fr": "Cabinet au Maârif."
    }
    assert clean_bio_i18n({"de": "Nein"}) == {}
    assert clean_bio_i18n(None) == {}
