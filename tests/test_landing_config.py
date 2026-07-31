"""Which page a doctor gets: the specialty's sections, and the chosen design.

Two decisions that must not contaminate each other. The template is inherited —
a dentist page is acts-first because they are a dentist. The design is picked by
staff at the visit, and it is the thing the doctor actually looks at, so it has
to survive the two events that quietly rewrite a page: a lapsed subscription,
and a design being retired from the landing app.
"""

import pytest
from sehaty.db import ClaimStatus, DoctorProfile, DoctorSpecialty, Specialty, User, UserRole
from sqlalchemy.orm import Session

from sehaty.core.controllers.landing_config import (
    DEFAULT_LAYOUT,
    LandingConfigController,
)
from sehaty.core.errors import SehatyValidationError


def _doctor(session: Session, specialty_slug: str = "dentistry") -> int:
    specialty = session.query(Specialty).filter_by(slug=specialty_slug).first()
    if specialty is None:
        specialty = Specialty(
            slug=specialty_slug, name_en=specialty_slug, name_fr="Dentiste", name_ar="د"
        )
        session.add(specialty)
        session.commit()

    user = User(email="landing-doc@import.invalid", role=UserRole.DOCTOR, is_active=True)
    session.add(user)
    session.commit()
    session.add(
        DoctorProfile(
            user_id=user.id,
            full_name="Dr Amina Bennani",
            slug="dr-amina-bennani-casablanca",
            license_no=f"LIC-{user.id}",
            city="Casablanca",
            claim_status=ClaimStatus.UNCLAIMED,
        )
    )
    session.add(DoctorSpecialty(doctor_id=user.id, specialty_id=specialty.id))
    session.commit()
    return int(user.id)


@pytest.mark.usefixtures("_pg_engine")
class TestLayout:
    def test_an_unchosen_design_is_the_one_already_published(self, pg_session: Session) -> None:
        """Every imported page is classic; nothing changes look on deploy."""
        config = LandingConfigController.for_doctor(_doctor(pg_session))

        assert config.layout == DEFAULT_LAYOUT == "classic"
        assert config.layout_is_default is True

    def test_the_design_is_independent_of_the_specialty_template(
        self, pg_session: Session
    ) -> None:
        """A dentist page stays acts-first whichever design it is built with."""
        doctor_id = _doctor(pg_session)

        config = LandingConfigController.upsert(doctor_id, layout="editorial")

        assert (config.layout, config.layout_is_default) == ("editorial", False)
        # Untouched, and still inherited rather than pinned by the layout save.
        assert (config.template, config.template_is_default) == ("dentistry", True)

    def test_the_design_survives_a_lapsed_subscription(self, pg_session: Session) -> None:
        """Content is what a subscription buys; the design is not.

        Restyling a page whose QR is printed on a plaque because a payment was
        late is a far worse surprise than a services list going quiet.
        """
        doctor_id = _doctor(pg_session)
        LandingConfigController.upsert(
            doctor_id, layout="compact", accent="#123456", tagline="Cabinet au Maârif."
        )
        LandingConfigController.set_personalized(doctor_id, enabled=True)

        lapsed = LandingConfigController.set_personalized(doctor_id, enabled=False)

        assert lapsed.layout == "compact"
        # The paid parts do go quiet.
        assert lapsed.accent is None
        assert lapsed.tagline is None

    def test_a_retired_design_reads_as_unchosen(self, pg_session: Session) -> None:
        """A key the landing app no longer ships must not 500 a published page."""
        doctor_id = _doctor(pg_session)
        LandingConfigController.upsert(doctor_id, layout="editorial")
        # Simulates the design being dropped from the landing app after a deploy.
        with pg_session.begin():
            pg_session.execute(
                DoctorProfile.__table__.metadata.tables["doctor_landings"]
                .update()
                .values(layout="brutalist")
            )

        config = LandingConfigController.for_doctor(doctor_id)

        assert config.layout == "classic"
        assert config.layout_is_default is True

    def test_an_unknown_design_is_refused_on_the_way_in(self, pg_session: Session) -> None:
        """Refused, not silently defaulted: the console must not claim a design
        the page does not have."""
        doctor_id = _doctor(pg_session)

        with pytest.raises(SehatyValidationError):
            LandingConfigController.upsert(doctor_id, layout="brutalist")
