"""Pure-SQLite tests for the public-landing fields on ``upsert_profile``.

Covers the cabinet contact numbers, the structured opening hours and the
insurance list — the three things the doctor's public page renders and the
500 MAD pack promises. The ``get_by_slug`` round-trip needs ``ST_X``/``ST_Y``
and lives in ``test_doctor_profile.py`` against a live PostGIS; persistence and
validation touch no geography function, so they run here on in-memory SQLite.

Same dialect-scoped ``Geography`` shim as the other SQLite suites: no
``lat``/``lng`` is ever passed, so ``geopoint`` stays NULL and is never read.
"""

import pytest
from geoalchemy2 import Geography
from geoalchemy2 import functions as geo_functions
from sehaty.db import DoctorProfile, DoctorSpecialty, Specialty, User, UserRole
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.doctors import DoctorController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError


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
]

# A typical Casablanca cabinet week: Monday and Tuesday, split at midday.
_MON_TUE = [
    {"weekday": 0, "ranges": [["09:00", "12:30"], ["15:00", "19:00"]]},
    {"weekday": 1, "ranges": [["09:00", "12:30"]]},
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


def _make_doctor(factory: sessionmaker[Session], email: str) -> int:
    with factory() as s:
        user = User(email=email, role=UserRole.DOCTOR, is_active=True)
        s.add(user)
        s.commit()
        return user.id


def _stored(factory: sessionmaker[Session], user_id: int, column):
    with factory() as s:
        return s.execute(select(column).where(DoctorProfile.user_id == user_id)).scalar_one()


# --- cabinet contact -------------------------------------------------------


def test_create_persists_cabinet_contact(db: sessionmaker[Session]) -> None:
    uid = _make_doctor(db, "contact@clinic.ma")

    DoctorController.upsert_profile(
        uid,
        full_name="Dr Contact",
        phone_fixe="+212522000000",
        phone_mobile="+212661000000",
        whatsapp="+212661000000",
    )

    assert _stored(db, uid, DoctorProfile.phone_fixe) == "+212522000000"
    assert _stored(db, uid, DoctorProfile.phone_mobile) == "+212661000000"
    assert _stored(db, uid, DoctorProfile.whatsapp) == "+212661000000"


def test_contact_numbers_are_independent_of_the_login_phone(db: sessionmaker[Session]) -> None:
    # The published cabinet number must never be sourced from User.phone, which
    # is the private login identity.
    uid = _make_doctor(db, "sep@clinic.ma")

    DoctorController.upsert_profile(uid, full_name="Dr Sep", phone_fixe="+212522111111")

    with db() as s:
        assert s.execute(select(User.phone).where(User.id == uid)).scalar_one() is None


# --- opening hours ---------------------------------------------------------


def test_create_persists_and_sorts_opening_hours(db: sessionmaker[Session]) -> None:
    uid = _make_doctor(db, "hours@clinic.ma")

    DoctorController.upsert_profile(
        uid, full_name="Dr Hours", opening_hours=list(reversed(_MON_TUE))
    )

    assert _stored(db, uid, DoctorProfile.opening_hours) == _MON_TUE


def test_create_defaults_opening_hours_to_empty_list(db: sessionmaker[Session]) -> None:
    uid = _make_doctor(db, "nohours@clinic.ma")

    DoctorController.upsert_profile(uid, full_name="Dr No Hours")

    assert _stored(db, uid, DoctorProfile.opening_hours) == []


def test_weekday_with_no_ranges_is_dropped_as_closed(db: sessionmaker[Session]) -> None:
    # "absent = closed" must stay the single convention, so an empty ranges list
    # is normalized away rather than stored.
    uid = _make_doctor(db, "closed@clinic.ma")

    DoctorController.upsert_profile(
        uid,
        full_name="Dr Closed",
        opening_hours=[
            {"weekday": 0, "ranges": [["09:00", "12:30"]]},
            {"weekday": 5, "ranges": []},
        ],
    )

    assert _stored(db, uid, DoctorProfile.opening_hours) == [
        {"weekday": 0, "ranges": [["09:00", "12:30"]]}
    ]


def test_update_none_retains_opening_hours(db: sessionmaker[Session]) -> None:
    uid = _make_doctor(db, "retainhours@clinic.ma")
    DoctorController.upsert_profile(uid, full_name="Dr Retain", opening_hours=_MON_TUE)

    DoctorController.upsert_profile(uid, full_name="Dr Retain", bio="updated")

    assert _stored(db, uid, DoctorProfile.opening_hours) == _MON_TUE


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param([{"weekday": 7, "ranges": [["09:00", "10:00"]]}], id="weekday-out-of-range"),
        pytest.param([{"weekday": "0", "ranges": [["09:00", "10:00"]]}], id="weekday-not-int"),
        pytest.param([{"weekday": 0, "ranges": [["9:00", "10:00"]]}], id="hour-not-zero-padded"),
        pytest.param([{"weekday": 0, "ranges": [["24:00", "25:00"]]}], id="hour-out-of-range"),
        pytest.param([{"weekday": 0, "ranges": [["12:00", "09:00"]]}], id="end-before-start"),
        pytest.param([{"weekday": 0, "ranges": [["09:00", "09:00"]]}], id="empty-range"),
        pytest.param([{"weekday": 0, "ranges": [["09:00", "10:00", "11:00"]]}], id="not-a-pair"),
        pytest.param(
            [{"weekday": 0, "ranges": [["09:00", "12:30"], ["11:00", "19:00"]]}], id="overlapping"
        ),
        pytest.param(
            [
                {"weekday": 0, "ranges": [["09:00", "12:30"]]},
                {"weekday": 0, "ranges": [["15:00", "19:00"]]},
            ],
            id="duplicate-weekday",
        ),
        pytest.param(["not-an-object"], id="entry-not-an-object"),
    ],
)
def test_invalid_opening_hours_raise(db: sessionmaker[Session], bad: list) -> None:
    uid = _make_doctor(db, "badhours@clinic.ma")

    with pytest.raises(SehatyValidationError):
        DoctorController.upsert_profile(uid, full_name="Dr Bad Hours", opening_hours=bad)


# --- insurance -------------------------------------------------------------


def test_insurances_are_lowercased_and_deduped_preserving_order(
    db: sessionmaker[Session],
) -> None:
    uid = _make_doctor(db, "ins@clinic.ma")

    DoctorController.upsert_profile(
        uid,
        full_name="Dr Ins",
        insurances=["CNSS", " cnops ", "cnss", "AMO", "  "],
    )

    assert _stored(db, uid, DoctorProfile.insurances) == ["cnss", "cnops", "amo"]


def test_create_defaults_tiers_payant_to_false(db: sessionmaker[Session]) -> None:
    uid = _make_doctor(db, "notp@clinic.ma")

    DoctorController.upsert_profile(uid, full_name="Dr No TP")

    assert _stored(db, uid, DoctorProfile.tiers_payant) is False


def test_update_none_retains_tiers_payant(db: sessionmaker[Session]) -> None:
    uid = _make_doctor(db, "retaintp@clinic.ma")
    DoctorController.upsert_profile(uid, full_name="Dr TP", tiers_payant=True)

    DoctorController.upsert_profile(uid, full_name="Dr TP", bio="updated")

    assert _stored(db, uid, DoctorProfile.tiers_payant) is True


class TestAdminPatchProfile:
    """Staff editing someone else's profile send only what they collected.

    `upsert_profile` is the doctor's own form and replaces wholesale; using it
    from the console would wipe a bio the operator never saw. These pin the
    partial semantics and the write whitelist.
    """

    def _doctor(self, factory: sessionmaker[Session]) -> int:
        uid = _make_doctor(factory, "patch@clinic.ma")
        DoctorController.upsert_profile(
            uid,
            full_name="Dr Patch",
            bio="Cardiologue depuis 2010",
            address="12 Bd Zerktouni",
            city="Casablanca",
            consultation_fee=400.0,
        )
        return uid

    def test_sets_only_the_fields_given(self, db: sessionmaker[Session]) -> None:
        uid = self._doctor(db)

        DoctorController.patch_profile(uid, opening_hours=_MON_TUE)

        assert _stored(db, uid, DoctorProfile.opening_hours) == _MON_TUE
        # Everything the operator did not touch survives.
        assert _stored(db, uid, DoctorProfile.bio) == "Cardiologue depuis 2010"
        assert _stored(db, uid, DoctorProfile.address) == "12 Bd Zerktouni"
        assert _stored(db, uid, DoctorProfile.consultation_fee) == 400.0

    def test_normalizes_insurances_like_the_doctor_form(self, db: sessionmaker[Session]) -> None:
        uid = self._doctor(db)
        DoctorController.patch_profile(uid, insurances=["CNSS", " cnops ", "CNSS"])
        assert _stored(db, uid, DoctorProfile.insurances) == ["cnss", "cnops"]

    def test_validates_opening_hours(self, db: sessionmaker[Session]) -> None:
        uid = self._doctor(db)
        with pytest.raises(SehatyValidationError):
            DoctorController.patch_profile(
                uid, opening_hours=[{"weekday": 9, "ranges": [["09:00", "10:00"]]}]
            )

    def test_refuses_to_write_protected_columns(self, db: sessionmaker[Session]) -> None:
        # verification_status, claim_status and slug are separate, audited
        # decisions — an onboarding form must not be able to reach them.
        uid = self._doctor(db)
        for field in ("verification_status", "claim_status", "slug", "license_no"):
            with pytest.raises(SehatyValidationError):
                DoctorController.patch_profile(uid, **{field: "x"})

    def test_unknown_doctor_raises(self, db: sessionmaker[Session]) -> None:
        with pytest.raises(SehatyNotFoundError):
            DoctorController.patch_profile(999_999, city="Casablanca")
