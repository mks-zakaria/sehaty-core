"""Auth core tests on an in-memory SQLite engine.

The PostGIS ``Geography`` column + gist index on ``DoctorProfile`` are not
buildable on stock SQLite, so this module registers dialect-scoped
compilation shims (geo type -> ``TEXT``; ``ST_GeogFromText`` -> pass-through)
purely for the test engine. That lets ``register_doctor`` run end-to-end; the
profile's ``verification_status`` is asserted with a column-only select
(a full-entity load would wrap ``geopoint`` in the missing ``AsBinary``).
"""

from datetime import UTC, datetime, timedelta

import pytest
from geoalchemy2 import Geography
from geoalchemy2 import functions as geo_functions
from sehaty.db import DoctorProfile, PhoneOtp, RefreshToken, User, UserRole, VerificationStatus
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core import security
from sehaty.core.controllers.auth import AuthController, MeView
from sehaty.core.db import session as session_mod
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyForbiddenError,
    SehatyValidationError,
)


@compiles(Geography, "sqlite")
def _geography_as_text_on_sqlite(element, compiler, **kw) -> str:
    # SQLite has no geography type; store the column as opaque TEXT for tests.
    return "TEXT"


@compiles(geo_functions.ST_GeogFromText, "sqlite")
def _geog_bind_passthrough_on_sqlite(element, compiler, **kw) -> str:
    # Skip the PostGIS constructor SQLite lacks; bind the raw value instead.
    return compiler.process(list(element.clauses)[0], **kw)


_TABLES = [
    User.__table__,
    RefreshToken.__table__,
    PhoneOtp.__table__,
    DoctorProfile.__table__,
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


def _make_user(factory: sessionmaker[Session], **kwargs) -> User:
    defaults = dict(role=UserRole.PATIENT, is_active=True)
    defaults.update(kwargs)
    with factory() as s:
        user = User(**defaults)
        s.add(user)
        s.commit()
        s.refresh(user)
        return user


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def test_password_hash_and_verify() -> None:
    h = security.hash_password("s3cret-pw")
    assert h != "s3cret-pw"
    assert h.startswith("$2b$")  # bcrypt
    assert security.verify_password("s3cret-pw", h)
    assert not security.verify_password("wrong", h)


def test_verify_password_tolerates_bad_hash() -> None:
    assert not security.verify_password("x", "not-a-bcrypt-hash")


# --------------------------------------------------------------------------- #
# Access tokens
# --------------------------------------------------------------------------- #
def test_access_token_round_trip() -> None:
    token = security.create_access_token(42, UserRole.DOCTOR)
    claims = security.decode_token(token)
    assert claims is not None
    assert claims["sub"] == "42"
    assert claims["role"] == "DOCTOR"


def test_expired_access_token_decodes_to_none() -> None:
    import jwt

    past = datetime.now(UTC) - timedelta(minutes=1)
    token = jwt.encode(
        {"sub": "1", "role": "PATIENT", "exp": past},
        security.JWT_SECRET,
        algorithm=security.JWT_ALGORITHM,
    )
    assert security.decode_token(token) is None


def test_tampered_token_decodes_to_none() -> None:
    assert security.decode_token("not.a.jwt") is None


# --------------------------------------------------------------------------- #
# Refresh-token rotation
# --------------------------------------------------------------------------- #
def test_refresh_rotation_invalidates_old(db: sessionmaker[Session]) -> None:
    user = _make_user(db, email="rot@x.io", phone="+212600000001")
    with db() as s:
        raw = security.issue_refresh_token(s, user.id)
        s.commit()
    with db() as s:
        new_raw = security.rotate_refresh_token(s, raw)
        s.commit()
    assert new_raw != raw
    # Replaying the old token after rotation is rejected.
    with db() as s, pytest.raises(SehatyForbiddenError):
        security.rotate_refresh_token(s, raw)
    # The freshly issued token still rotates.
    with db() as s:
        security.rotate_refresh_token(s, new_raw)
        s.commit()


def test_revoke_all_logs_out(db: sessionmaker[Session]) -> None:
    user = _make_user(db, email="ra@x.io", phone="+212600000002")
    with db() as s:
        raw1 = security.issue_refresh_token(s, user.id)
        raw2 = security.issue_refresh_token(s, user.id)
        s.commit()
    with db() as s:
        security.revoke_all(s, user.id)
        s.commit()
    for raw in (raw1, raw2):
        with db() as s, pytest.raises(SehatyForbiddenError):
            security.rotate_refresh_token(s, raw)


def test_expired_refresh_token_rejected(db: sessionmaker[Session]) -> None:
    user = _make_user(db, email="exp@x.io", phone="+212600000003")
    with db() as s:
        raw = security.issue_refresh_token(s, user.id)
        token = s.execute(
            select(RefreshToken).where(RefreshToken.token_hash == security.hash_token(raw))
        ).scalar_one()
        token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    with db() as s, pytest.raises(SehatyForbiddenError):
        security.rotate_refresh_token(s, raw)


# --------------------------------------------------------------------------- #
# OTP
# --------------------------------------------------------------------------- #
def test_otp_happy_path(db: sessionmaker[Session]) -> None:
    with db() as s:
        code = security.generate_otp(s, "+212611111111")
        s.commit()
    assert len(code) == 6 and code.isdigit()
    with db() as s:
        otp = security.verify_otp(s, "+212611111111", code)
        s.commit()
        assert otp.consumed_at is not None
    # A consumed OTP cannot be reused.
    with db() as s, pytest.raises(SehatyValidationError):
        security.verify_otp(s, "+212611111111", code)


def test_otp_wrong_code_increments_attempts(db: sessionmaker[Session]) -> None:
    with db() as s:
        security.generate_otp(s, "+212622222222")
        s.commit()
    with db() as s, pytest.raises(SehatyValidationError):
        security.verify_otp(s, "+212622222222", "000000")
    # verify_otp commits the attempt increment itself, so it survives here.
    with db() as s:
        otp = s.execute(select(PhoneOtp).where(PhoneOtp.phone == "+212622222222")).scalar_one()
        assert otp.attempts == 1


def test_otp_expired_fails(db: sessionmaker[Session]) -> None:
    with db() as s:
        code = security.generate_otp(s, "+212633333333")
        otp = s.execute(select(PhoneOtp).where(PhoneOtp.phone == "+212633333333")).scalar_one()
        otp.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        s.commit()
    with db() as s, pytest.raises(SehatyValidationError):
        security.verify_otp(s, "+212633333333", code)


def test_otp_locks_out_after_max_attempts(db: sessionmaker[Session]) -> None:
    phone = "+212644444444"
    with db() as s:
        code = security.generate_otp(s, phone)
        s.commit()
    # Five wrong attempts -> attempts reaches the cap (each verify_otp commits
    # its own increment).
    for _ in range(security.OTP_MAX_ATTEMPTS):
        with db() as s, pytest.raises(SehatyValidationError):
            security.verify_otp(s, phone, "000000")
    # The next attempt (even with the CORRECT code) is locked out.
    with db() as s, pytest.raises(SehatyForbiddenError):
        security.verify_otp(s, phone, code)


# --------------------------------------------------------------------------- #
# Controllers
# --------------------------------------------------------------------------- #
def test_register_doctor_is_unverified(db: sessionmaker[Session]) -> None:
    user = AuthController.register_doctor(
        email="doc@clinic.ma",
        password="pw-doctor-123",
        full_name="Dr Amina",
        slug="dr-amina",
        license_no="LIC-001",
    )
    assert user.role == UserRole.DOCTOR
    with db() as s:
        stored = s.execute(select(User).where(User.email == "doc@clinic.ma")).scalar_one()
        assert stored.password_hash is not None
        assert stored.password_hash != "pw-doctor-123"  # password is hashed
        # Unverified: the doctor profile starts PENDING (column-only select to
        # avoid loading the geopoint column).
        status = s.execute(
            select(DoctorProfile.verification_status).where(DoctorProfile.user_id == stored.id)
        ).scalar_one()
        assert status == VerificationStatus.PENDING


def test_login_rejects_wrong_password(db: sessionmaker[Session]) -> None:
    _make_user(
        db,
        email="login@x.io",
        role=UserRole.DOCTOR,
        password_hash=security.hash_password("correct-pw"),
    )
    with pytest.raises(SehatyForbiddenError):
        AuthController.login("login@x.io", "wrong-pw")
    bundle = AuthController.login("login@x.io", "correct-pw")
    assert set(bundle) == {"access", "refresh", "role"}
    assert bundle["role"] == UserRole.DOCTOR
    assert security.decode_token(bundle["access"]) is not None


def test_login_rejects_inactive_user(db: sessionmaker[Session]) -> None:
    _make_user(
        db,
        email="off@x.io",
        role=UserRole.DOCTOR,
        is_active=False,
        password_hash=security.hash_password("pw"),
    )
    with pytest.raises(SehatyForbiddenError):
        AuthController.login("off@x.io", "pw")


def test_patient_otp_login_and_refresh(db: sessionmaker[Session]) -> None:
    phone = "+212655555555"
    code = AuthController.request_patient_otp(phone)
    bundle = AuthController.verify_patient_otp_and_login(phone, code)
    assert bundle["role"] == UserRole.PATIENT
    # A patient user was created for the phone.
    with db() as s:
        user = s.execute(select(User).where(User.phone == phone)).scalar_one()
        assert user.role == UserRole.PATIENT

    # Refresh rotates and returns a working new pair.
    new_bundle = AuthController.refresh(bundle["refresh"])
    assert new_bundle["refresh"] != bundle["refresh"]
    assert security.decode_token(new_bundle["access"]) is not None
    # Old refresh token is now invalid.
    with pytest.raises(SehatyForbiddenError):
        AuthController.refresh(bundle["refresh"])


def test_logout_revokes_refresh(db: sessionmaker[Session]) -> None:
    user = _make_user(db, email="lo@x.io", phone="+212666666666")
    with db() as s:
        raw = security.issue_refresh_token(s, user.id)
        s.commit()
    AuthController.logout(raw)
    with pytest.raises(SehatyForbiddenError):
        AuthController.refresh(raw)


def test_get_active_user(db: sessionmaker[Session]) -> None:
    user = _make_user(db, email="ga@x.io", phone="+212677777777")
    assert AuthController.get_active_user(user.id).id == user.id
    with pytest.raises(SehatyForbiddenError):
        AuthController.get_active_user(999999)


def test_patient_register_and_login(db: sessionmaker[Session]) -> None:
    bundle = AuthController.register_patient("+212650000001", "secret123")
    assert bundle["role"] == UserRole.PATIENT
    assert bundle["access"] and bundle["refresh"]

    # Log in with the same phone + password.
    login = AuthController.login_patient("+212650000001", "secret123")
    assert login["role"] == UserRole.PATIENT

    # Wrong password -> forbidden (opaque).
    with pytest.raises(SehatyForbiddenError):
        AuthController.login_patient("+212650000001", "nope")


def test_patient_register_duplicate_phone_conflicts(db: sessionmaker[Session]) -> None:
    AuthController.register_patient("+212650000002", "secret123")
    with pytest.raises(SehatyConflictError):
        AuthController.register_patient("+212650000002", "another123")


def test_patient_register_validates(db: sessionmaker[Session]) -> None:
    with pytest.raises(SehatyValidationError):
        AuthController.register_patient("  ", "secret123")
    with pytest.raises(SehatyValidationError):
        AuthController.register_patient("+212650000003", "short")


def test_register_upgrades_passwordless_phone(db: sessionmaker[Session]) -> None:
    # Simulate an OTP-created passwordless patient, then register a password.
    with db() as s:
        s.add(
            User(
                email="phone++212650000004@sehaty.local",
                phone="+212650000004",
                role=UserRole.PATIENT,
                is_active=True,
            )
        )
        s.commit()
    bundle = AuthController.register_patient("+212650000004", "secret123")
    assert bundle["role"] == UserRole.PATIENT
    assert AuthController.login_patient("+212650000004", "secret123")["access"]


class TestPlaceholderEmail:
    """A synthesised address must never be shown back as the user's own email.

    Phone-registered patients get ``phone+2126...@sehaty.local`` so the NOT NULL
    and unique constraints on ``User.email`` hold. That string is not an address
    anybody can be reached at, and the account screen was rendering it under
    "Email" — worse than showing nothing, because a patient cannot tell it is
    not real.
    """

    def _user(self, email: str | None):
        class _U:
            id = 1
            role = "PATIENT"
            phone = "+212708009645"

        _U.email = email
        return _U()

    def test_synthesised_phone_email_is_hidden(self) -> None:
        view = MeView.model_validate(self._user("phone+212708009645@sehaty.local"))
        assert view.email is None
        # The phone is the real contact detail and must survive.
        assert view.phone == "+212708009645"

    def test_imported_doctor_placeholder_is_hidden(self) -> None:
        assert MeView.model_validate(self._user("dr-amina@import.invalid")).email is None

    def test_a_real_address_is_preserved(self) -> None:
        assert MeView.model_validate(self._user("amina@gmail.com")).email == "amina@gmail.com"

    def test_matching_is_case_insensitive(self) -> None:
        assert MeView.model_validate(self._user("Phone+212@SEHATY.LOCAL")).email is None

    def test_a_lookalike_real_domain_is_not_hidden(self) -> None:
        # Only the exact placeholder domains are hidden — not anything that
        # merely contains the word.
        assert (
            MeView.model_validate(self._user("me@sehaty.localhost.ma")).email
            == "me@sehaty.localhost.ma"
        )

    def test_absent_email_stays_absent(self) -> None:
        assert MeView.model_validate(self._user(None)).email is None
