"""Doctor-profile integration tests against a live PostGIS.

These exercise the geography round-trip that SQLite cannot fake: ``WKTElement``
writes on upsert and ``ST_X``/``ST_Y`` reads on ``get_by_slug``. They take the
``pg_session`` fixture (see ``conftest.py``) and SKIP when no database is
reachable. The pure-logic SQLite suites (``test_admin``/``test_auth``/…) are
untouched and keep running everywhere.
"""

import pytest
from sehaty.db import DoctorProfile, Specialty, User, UserRole, VerificationStatus
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from sehaty.core.controllers.doctors import DoctorController
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError

# Casablanca-ish coordinates (lon/lat, WGS84).
_LAT = 33.5731104
_LNG = -7.5898434
_EPS = 1e-6


def _make_doctor(session: Session, email: str) -> int:
    """Create a DOCTOR user (no profile yet) and return its id."""
    user = User(email=email, role=UserRole.DOCTOR, is_active=True)
    session.add(user)
    session.commit()
    return user.id


def _make_patient(session: Session, email: str) -> int:
    user = User(email=email, role=UserRole.PATIENT, is_active=True)
    session.add(user)
    session.commit()
    return user.id


def _verify(session: Session, user_id: int) -> None:
    """Mark a doctor VERIFIED so ``get_by_slug`` will surface them."""
    session.execute(
        update(DoctorProfile)
        .where(DoctorProfile.user_id == user_id)
        .values(verification_status=VerificationStatus.VERIFIED)
    )
    session.commit()


def _seed_specialties(session: Session, *slugs: str) -> None:
    for slug in slugs:
        session.add(Specialty(slug=slug, name_en=slug, name_fr=slug, name_ar=slug))
    session.commit()


def test_upsert_persists_point_and_roundtrips(pg_session: Session) -> None:
    uid = _make_doctor(pg_session, "geo@clinic.ma")

    slug = DoctorController.upsert_profile(
        uid,
        full_name="Dr Amina Bennani",
        bio="Cardiologue à Casablanca",
        city="Casablanca",
        lat=_LAT,
        lng=_LNG,
        consultation_fee=350.0,
    )
    _verify(pg_session, uid)

    view = DoctorController.get_by_slug(slug)

    assert view.slug == slug
    assert view.full_name == "Dr Amina Bennani"
    assert view.city == "Casablanca"
    assert view.consultation_fee == 350.0
    # The point survives the geography round-trip within float tolerance.
    assert view.lat == pytest.approx(_LAT, abs=_EPS)
    assert view.lng == pytest.approx(_LNG, abs=_EPS)


def test_slug_unique_across_same_name(pg_session: Session) -> None:
    a = _make_doctor(pg_session, "a@clinic.ma")
    b = _make_doctor(pg_session, "b@clinic.ma")

    slug_a = DoctorController.upsert_profile(a, full_name="Dr Yassine Alaoui")
    slug_b = DoctorController.upsert_profile(b, full_name="Dr Yassine Alaoui")

    assert slug_a == "dr-yassine-alaoui"
    assert slug_b == "dr-yassine-alaoui-2"
    assert slug_a != slug_b


def test_update_keeps_original_slug(pg_session: Session) -> None:
    uid = _make_doctor(pg_session, "keep@clinic.ma")

    first = DoctorController.upsert_profile(uid, full_name="Dr Salma Idrissi")
    # A later update with a *different* name must not change the slug.
    second = DoctorController.upsert_profile(
        uid, full_name="Professor Salma Idrissi", bio="updated"
    )

    assert first == second == "dr-salma-idrissi"
    stored = pg_session.execute(
        select(DoctorProfile.full_name, DoctorProfile.bio).where(DoctorProfile.user_id == uid)
    ).one()
    assert stored.full_name == "Professor Salma Idrissi"
    assert stored.bio == "updated"


def test_specialty_slugs_link_and_replace(pg_session: Session) -> None:
    _seed_specialties(pg_session, "cardiology", "dermatology", "pediatrics")
    uid = _make_doctor(pg_session, "spec@clinic.ma")

    DoctorController.upsert_profile(
        uid,
        full_name="Dr Nadia Cherkaoui",
        specialty_slugs=["cardiology", "dermatology"],
    )
    _verify(pg_session, uid)
    view = DoctorController.get_by_slug("dr-nadia-cherkaoui")
    assert {s.slug for s in view.specialties} == {"cardiology", "dermatology"}

    # Re-upsert with a different set fully replaces the links.
    DoctorController.upsert_profile(
        uid,
        full_name="Dr Nadia Cherkaoui",
        specialty_slugs=["pediatrics"],
    )
    view = DoctorController.get_by_slug("dr-nadia-cherkaoui")
    assert {s.slug for s in view.specialties} == {"pediatrics"}


def test_unknown_specialty_slug_raises(pg_session: Session) -> None:
    _seed_specialties(pg_session, "cardiology")
    uid = _make_doctor(pg_session, "bad@clinic.ma")

    with pytest.raises(SehatyValidationError):
        DoctorController.upsert_profile(
            uid,
            full_name="Dr Test",
            specialty_slugs=["cardiology", "does-not-exist"],
        )


def test_get_by_slug_unverified_not_found(pg_session: Session) -> None:
    uid = _make_doctor(pg_session, "pending@clinic.ma")
    slug = DoctorController.upsert_profile(uid, full_name="Dr Pending")

    # Profile exists but is still PENDING → never surfaced publicly.
    with pytest.raises(SehatyNotFoundError):
        DoctorController.get_by_slug(slug)


def test_get_by_slug_missing_not_found(pg_session: Session) -> None:
    with pytest.raises(SehatyNotFoundError):
        DoctorController.get_by_slug("nobody-here")


def test_upsert_non_doctor_raises(pg_session: Session) -> None:
    pid = _make_patient(pg_session, "patient@clinic.ma")

    with pytest.raises(SehatyNotFoundError):
        DoctorController.upsert_profile(pid, full_name="Not A Doctor")
