"""Authentication business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). Each
method owns one transaction via ``get_session()`` and composes the primitives
in ``sehaty.core.security``. Failures raise the ``SehatyError`` taxonomy;
methods never return ``None`` to signal an error.

Two entry paths:

* doctors register with email + password and log in with them;
* patients authenticate passwordlessly via a phone OTP.
"""

from sehaty.db import DoctorProfile, RefreshToken, User, UserRole, VerificationStatus
from sqlalchemy import select

from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyForbiddenError,
    SehatyValidationError,
)
from sehaty.core.security import (
    create_access_token,
    generate_otp,
    hash_password,
    hash_token,
    issue_refresh_token,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_otp,
    verify_password,
)


def _token_bundle(user: User, refresh: str) -> dict:
    return {
        "access": create_access_token(user.id, user.role),
        "refresh": refresh,
        "role": user.role,
    }


class AuthController:
    @staticmethod
    def register_doctor(
        *,
        email: str,
        password: str,
        full_name: str,
        slug: str,
        license_no: str,
        phone: str | None = None,
    ) -> User:
        """Create a DOCTOR ``User`` + pending ``DoctorProfile``.

        The account is created unverified: the profile's
        ``verification_status`` starts ``PENDING`` (an admin later verifies the
        licence). Returns the created (detached) ``User``.
        """
        if not email.strip() or not password:
            raise SehatyValidationError("email and password are required")
        with get_session() as session:
            exists = session.execute(
                select(User.id).where(User.email == email)
            ).scalar_one_or_none()
            if exists is not None:
                raise SehatyConflictError("email already registered")
            user = User(
                email=email,
                phone=phone,
                password_hash=hash_password(password),
                role=UserRole.DOCTOR,
                is_active=True,
            )
            session.add(user)
            session.flush()
            session.add(
                DoctorProfile(
                    user_id=user.id,
                    full_name=full_name,
                    slug=slug,
                    license_no=license_no,
                    verification_status=VerificationStatus.PENDING,
                )
            )
            session.flush()
            return user

    @staticmethod
    def login(email: str, password: str, user_agent: str | None = None) -> dict:
        """Verify email/password and issue access + refresh tokens.

        Rejects a wrong password or a disabled account with ``Forbidden``
        (identical error for both so we don't leak which emails exist).
        """
        with get_session() as session:
            user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if (
                user is None
                or user.password_hash is None
                or not verify_password(password, user.password_hash)
            ):
                raise SehatyForbiddenError("invalid credentials")
            if not user.is_active:
                raise SehatyForbiddenError("account is disabled")
            return _token_bundle(user, issue_refresh_token(session, user.id, user_agent))

    @staticmethod
    def request_patient_otp(phone: str) -> str:
        """Generate a phone OTP and return the code for the SMS layer."""
        if not phone.strip():
            raise SehatyValidationError("phone is required")
        with get_session() as session:
            return generate_otp(session, phone)

    @staticmethod
    def verify_patient_otp_and_login(phone: str, code: str, user_agent: str | None = None) -> dict:
        """Verify a phone OTP, get-or-create the PATIENT user, and log in."""
        with get_session() as session:
            verify_otp(session, phone, code)
            user = session.execute(select(User).where(User.phone == phone)).scalar_one_or_none()
            if user is None:
                # Passwordless patient: email is required + unique on User, so
                # synthesise a stable placeholder from the phone number.
                user = User(
                    email=f"phone+{phone}@sehaty.local",
                    phone=phone,
                    role=UserRole.PATIENT,
                    is_active=True,
                )
                session.add(user)
                session.flush()
            if not user.is_active:
                raise SehatyForbiddenError("account is disabled")
            return _token_bundle(user, issue_refresh_token(session, user.id, user_agent))

    @staticmethod
    def refresh(raw: str) -> dict:
        """Rotate a refresh token and mint a fresh access + refresh pair."""
        with get_session() as session:
            new_raw = rotate_refresh_token(session, raw)
            new_token = session.execute(
                select(RefreshToken).where(RefreshToken.token_hash == hash_token(new_raw))
            ).scalar_one()
            user = session.get(User, new_token.user_id)
            if user is None or not user.is_active:
                raise SehatyForbiddenError("user not found or inactive")
            return _token_bundle(user, new_raw)

    @staticmethod
    def logout(raw: str) -> None:
        """Revoke a single refresh token (idempotent)."""
        with get_session() as session:
            revoke_refresh_token(session, raw)

    @staticmethod
    def get_active_user(user_id: int) -> User:
        """Return the active user or raise ``Forbidden`` if missing/inactive."""
        with get_session() as session:
            user = session.get(User, user_id)
            if user is None or not user.is_active:
                raise SehatyForbiddenError("user not found or inactive")
            return user
