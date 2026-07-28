"""Doctor-profile integration tests against a live PostGIS.

These exercise the geography round-trip that SQLite cannot fake: ``WKTElement``
writes on upsert and ``ST_X``/``ST_Y`` reads on ``get_by_slug``. They take the
``pg_session`` fixture (see ``conftest.py``) and SKIP when no database is
reachable. The pure-logic SQLite suites (``test_admin``/``test_auth``/…) are
untouched and keep running everywhere.
"""

from datetime import UTC, date, datetime, time

import pytest
from sehaty.db import (
    Availability,
    ClaimStatus,
    DoctorProfile,
    GeoPrecision,
    Plan,
    Specialty,
    Subscription,
    SubscriptionStatus,
    User,
    UserRole,
    VerificationStatus,
)
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from sehaty.core.controllers.availability import (
    AvailabilityController,
    mirror_opening_hours,
)
from sehaty.core.controllers.claims import grant_access
from sehaty.core.controllers.doctors import DoctorController
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyNotFoundError,
    SehatyValidationError,
)
from sehaty.core.security import verify_password

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


def _subscribe(session: Session, user_id: int) -> None:
    """Give a doctor a live subscription so the booking engine is switched on.

    Slot generation is gated on entitlement: a doctor with no subscription keeps
    their public page but has no bookable agenda.
    """
    plan = Plan(code=f"plan-{user_id}", name="Basic", price_month=199.0, currency="MAD")
    session.add(plan)
    session.flush()
    session.add(
        Subscription(
            doctor_id=user_id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=datetime(2026, 7, 1, tzinfo=UTC),
            current_period_end=datetime(2027, 7, 1, tzinfo=UTC),
        )
    )
    session.commit()


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
        languages=["ar", "fr"],
        timezone="Europe/Paris",
    )
    _verify(pg_session, uid)

    view = DoctorController.get_by_slug(slug)

    # The public view exposes the doctor's numeric user id so the
    # ``/dr/:slug`` → ``/book`` flow can obtain the booking ``doctor_id``.
    assert view.id == uid
    assert view.slug == slug
    assert view.full_name == "Dr Amina Bennani"
    assert view.city == "Casablanca"
    assert view.consultation_fee == 350.0
    # The point survives the geography round-trip within float tolerance.
    assert view.lat == pytest.approx(_LAT, abs=_EPS)
    assert view.lng == pytest.approx(_LNG, abs=_EPS)
    # Spoken languages are persisted and surfaced by the public view.
    assert view.languages == ["ar", "fr"]
    # The clinic timezone is persisted and surfaced by the public view.
    assert view.timezone == "Europe/Paris"


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
    assert view.id == uid
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


_MONDAY = date(2026, 8, 3)
assert _MONDAY.weekday() == 0


def test_get_public_slots_returns_slots_for_verified(pg_session: Session) -> None:
    uid = _make_doctor(pg_session, "slots@clinic.ma")
    slug = DoctorController.upsert_profile(uid, full_name="Dr Slots")
    _verify(pg_session, uid)
    _subscribe(pg_session, uid)
    # Monday 09:00-11:00, 30-min slots -> 4 bookable slots.
    pg_session.add(
        Availability(
            doctor_id=uid,
            weekday=_MONDAY.weekday(),
            start_time=time(9, 0),
            end_time=time(11, 0),
            slot_minutes=30,
        )
    )
    pg_session.commit()

    slots = DoctorController.get_public_slots(slug, _MONDAY, _MONDAY)

    assert len(slots) == 4
    # The profile's default timezone is Africa/Casablanca (UTC+1 in August), so
    # the 09:00 local window starts at 08:00Z — tz-correct slot generation.
    assert slots[0] == {
        "start_at": datetime(2026, 8, 3, 8, 0, tzinfo=UTC),
        "end_at": datetime(2026, 8, 3, 8, 30, tzinfo=UTC),
    }


def test_get_public_slots_pending_not_found(pg_session: Session) -> None:
    uid = _make_doctor(pg_session, "pendingslots@clinic.ma")
    slug = DoctorController.upsert_profile(uid, full_name="Dr Pending Slots")

    # PENDING profile is never surfaced publicly (no existence leak).
    with pytest.raises(SehatyNotFoundError):
        DoctorController.get_public_slots(slug, _MONDAY, _MONDAY)


def test_upsert_non_doctor_raises(pg_session: Session) -> None:
    pid = _make_patient(pg_session, "patient@clinic.ma")

    with pytest.raises(SehatyNotFoundError):
        DoctorController.upsert_profile(pid, full_name="Not A Doctor")


def test_dropping_a_pin_at_onboarding_upgrades_an_approximate_point(
    pg_session: Session,
) -> None:
    """The whole reason onboarding captures a location.

    An imported doctor is geocoded from a written address, which in these
    quartiers can only ever resolve to the town — every cabinet in Errahma
    lands on one shared pin. Standing outside the cabinet and dropping a real
    one is the only way that becomes navigable, so the precision has to follow
    the coordinates rather than stay stuck at APPROXIMATE.
    """
    uid = _make_doctor(pg_session, "pin@clinic.ma")
    DoctorController.upsert_profile(
        uid, full_name="Dr Imane Guerram", city="Casablanca", lat=33.5343, lng=-7.7322
    )
    with pg_session.begin():
        pg_session.execute(
            update(DoctorProfile)
            .where(DoctorProfile.user_id == uid)
            .values(geo_precision=GeoPrecision.APPROXIMATE)
        )

    DoctorController.patch_profile(uid, lat=_LAT, lng=_LNG)

    view = DoctorController.get_for_admin(uid)
    assert view.lat == pytest.approx(_LAT, abs=_EPS)
    assert view.geo_precision == str(GeoPrecision.EXACT)


def test_patching_other_fields_leaves_the_pin_and_its_precision_alone(
    pg_session: Session,
) -> None:
    """Editing a phone number must not silently promote a geocoded centroid."""
    uid = _make_doctor(pg_session, "nopin@clinic.ma")
    DoctorController.upsert_profile(
        uid, full_name="Dr Sara Bentass", city="Casablanca", lat=33.5343, lng=-7.7322
    )
    with pg_session.begin():
        pg_session.execute(
            update(DoctorProfile)
            .where(DoctorProfile.user_id == uid)
            .values(geo_precision=GeoPrecision.APPROXIMATE)
        )

    DoctorController.patch_profile(uid, phone_mobile="+212661000000")

    view = DoctorController.get_for_admin(uid)
    assert view.geo_precision == str(GeoPrecision.APPROXIMATE)


def test_granting_access_keeps_the_page_the_plaque_points_at(pg_session: Session) -> None:
    """The onboarding visit's last step, and the one that had no path.

    An imported doctor has a placeholder address and no password. Registering
    afresh would mint a second profile under a different slug, leaving the
    printed QR aimed at the abandoned one — so access has to attach to the
    profile that already exists.
    """
    uid = _make_doctor(pg_session, "import-placeholder@import.invalid")
    slug = DoctorController.upsert_profile(uid, full_name="Dr Imane Guerram", city="Casablanca")
    with pg_session.begin():
        pg_session.execute(
            update(DoctorProfile)
            .where(DoctorProfile.user_id == uid)
            .values(claim_status=ClaimStatus.UNCLAIMED)
        )

    grant = grant_access(uid, email="imane.guerram@gmail.com", password="cabinet-2026")

    assert grant.slug == slug
    assert grant.claim_status == str(ClaimStatus.CLAIMED)
    with pg_session.begin():
        user = pg_session.get(User, uid)
        assert user.email == "imane.guerram@gmail.com"
        assert verify_password("cabinet-2026", user.password_hash)


def test_granting_access_refuses_an_address_someone_else_holds(pg_session: Session) -> None:
    first = _make_doctor(pg_session, "taken@clinic.ma")
    DoctorController.upsert_profile(first, full_name="Dr A", city="Casablanca")
    second = _make_doctor(pg_session, "other@import.invalid")
    DoctorController.upsert_profile(second, full_name="Dr B", city="Casablanca")

    with pytest.raises(SehatyConflictError):
        grant_access(second, email="taken@clinic.ma", password="cabinet-2026")


def test_granting_access_refuses_a_delisted_page(pg_session: Session) -> None:
    """A removal is a tombstone: it must not be reopened by selling to them."""
    uid = _make_doctor(pg_session, "gone@import.invalid")
    DoctorController.upsert_profile(uid, full_name="Dr Gone", city="Casablanca")
    with pg_session.begin():
        pg_session.execute(
            update(DoctorProfile)
            .where(DoctorProfile.user_id == uid)
            .values(claim_status=ClaimStatus.REMOVAL_REQUESTED)
        )

    with pytest.raises(SehatyValidationError):
        grant_access(uid, email="gone@gmail.com", password="cabinet-2026")


def test_opening_hours_become_a_bookable_agenda(pg_session: Session) -> None:
    """The gap that makes a sold booking engine offer nothing.

    opening_hours is what the page displays; availabilities is what generates
    slots. A doctor states one set of hours and means both, so onboarding has to
    copy them across — otherwise the agenda is empty and nobody finds out until
    a patient tries to book.
    """
    uid = _make_doctor(pg_session, "hours@clinic.ma")
    DoctorController.upsert_profile(uid, full_name="Dr Hours", city="Casablanca")
    DoctorController.patch_profile(
        uid,
        opening_hours=[
            {"weekday": 0, "ranges": [["09:00", "12:30"], ["15:00", "19:00"]]},
            {"weekday": 5, "ranges": [["09:00", "13:00"]]},
        ],
    )

    created = mirror_opening_hours(uid, slot_minutes=30)

    assert created == 3
    rows = AvailabilityController.list(uid)
    assert sorted({r.weekday for r in rows}) == [0, 5]


def test_mirroring_twice_does_not_double_the_agenda(pg_session: Session) -> None:
    """An operator correcting hours in front of the doctor expects a fix."""
    uid = _make_doctor(pg_session, "twice@clinic.ma")
    DoctorController.upsert_profile(uid, full_name="Dr Twice", city="Casablanca")
    DoctorController.patch_profile(
        uid, opening_hours=[{"weekday": 1, "ranges": [["09:00", "12:00"]]}]
    )

    mirror_opening_hours(uid)
    assert mirror_opening_hours(uid) == 1
    assert len(AvailabilityController.list(uid)) == 1


def test_a_listed_page_is_public_but_never_badged(pg_session: Session) -> None:
    """The rule: publishing a directory entry is not vouching for a licence.

    The importer marked every page VERIFIED because the public read refused to
    render anything else, which put a "Vérifié" badge on thousands of doctors
    nobody had spoken to. LISTED renders identically and carries no claim.
    """
    uid = _make_doctor(pg_session, "listed@clinic.ma")
    slug = DoctorController.upsert_profile(
        uid, full_name="Dr Listed", city="Casablanca", lat=_LAT, lng=_LNG
    )
    with pg_session.begin():
        pg_session.execute(
            update(DoctorProfile)
            .where(DoctorProfile.user_id == uid)
            .values(verification_status=VerificationStatus.LISTED)
        )

    view = DoctorController.get_by_slug(slug)

    # Visible to a patient …
    assert view.slug == slug
    # … and honest about what we actually know.
    assert view.verification_status == str(VerificationStatus.LISTED)
    assert view.verification_status != str(VerificationStatus.VERIFIED)


def test_accreditation_is_what_grants_the_badge(pg_session: Session) -> None:
    """Only a human deciding this professional is who they say."""
    uid = _make_doctor(pg_session, "accredit@clinic.ma")
    slug = DoctorController.upsert_profile(
        uid, full_name="Dr Accredited", city="Casablanca", lat=_LAT, lng=_LNG
    )
    with pg_session.begin():
        pg_session.execute(
            update(DoctorProfile)
            .where(DoctorProfile.user_id == uid)
            .values(verification_status=VerificationStatus.LISTED)
        )
    assert DoctorController.get_by_slug(slug).verification_status == str(VerificationStatus.LISTED)

    _verify(pg_session, uid)

    assert DoctorController.get_by_slug(slug).verification_status == str(
        VerificationStatus.VERIFIED
    )


def test_a_pending_page_is_still_not_public(pg_session: Session) -> None:
    """Widening visibility to LISTED must not have opened it to everything."""
    uid = _make_doctor(pg_session, "pending@clinic.ma")
    slug = DoctorController.upsert_profile(
        uid, full_name="Dr Pending", city="Casablanca", lat=_LAT, lng=_LNG
    )
    with pg_session.begin():
        pg_session.execute(
            update(DoctorProfile)
            .where(DoctorProfile.user_id == uid)
            .values(verification_status=VerificationStatus.PENDING)
        )

    with pytest.raises(SehatyNotFoundError):
        DoctorController.get_by_slug(slug)
