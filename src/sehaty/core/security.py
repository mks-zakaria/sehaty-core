"""Auth primitives: password hashing, JWT access tokens, refresh-token
rotation, and phone OTP.

Secrets on the wire are never stored in the clear:

* passwords -> bcrypt (salted, work factor >= 12);
* refresh tokens -> opaque random string returned to the caller once, only
  ``sha256(token)`` persisted so a DB leak cannot mint sessions;
* OTP codes -> same, ``sha256(code)`` persisted; the plaintext is returned to
  the SMS layer and must never be logged.

Every IO function takes an explicit ``Session`` so callers own the
transaction boundary (and tests can inject an in-memory engine).
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from sehaty.db import PhoneOtp, RefreshToken
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from sehaty.core.config import (
    ACCESS_TOKEN_TTL_MINUTES,
    BCRYPT_ROUNDS,
    JWT_ALGORITHM,
    JWT_SECRET,
    OTP_MAX_ATTEMPTS,
    OTP_TTL_MINUTES,
    REFRESH_TOKEN_TTL_DAYS,
)
from sehaty.core.errors import SehatyForbiddenError, SehatyValidationError


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    """Normalise a possibly-naive timestamp to UTC-aware.

    Postgres round-trips tz-aware datetimes; SQLite (used in tests) drops the
    tzinfo. Normalising here keeps expiry comparisons total across both.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def hash_token(raw: str) -> str:
    """Return the hex ``sha256`` digest used as the stored lookup key."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt (salted, work factor >= 12)."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode(
        "utf-8"
    )


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/legacy hash -> treat as a failed login, never crash.
        return False


# --------------------------------------------------------------------------- #
# JWT access tokens
# --------------------------------------------------------------------------- #
def create_access_token(user_id: int, role: str) -> str:
    """Mint a short-lived HS256 access token for ``user_id`` with ``role``."""
    now = _utcnow()
    payload = {
        "sub": str(user_id),
        "role": str(role),
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": now + timedelta(minutes=ACCESS_TOKEN_TTL_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict | None:
    """Decode + verify an access token. Return ``None`` if invalid/expired."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


# --------------------------------------------------------------------------- #
# Refresh tokens (opaque, stored hashed, rotated on use)
# --------------------------------------------------------------------------- #
def issue_refresh_token(session: Session, user_id: int, user_agent: str | None = None) -> str:
    """Create a refresh token for ``user_id`` and return the RAW value.

    Only ``sha256(raw)`` is persisted; the raw string is shown to the caller
    exactly once.
    """
    raw = secrets.token_urlsafe(48)
    session.add(
        RefreshToken(
            user_id=user_id,
            token_hash=hash_token(raw),
            user_agent=user_agent,
            expires_at=_utcnow() + timedelta(days=REFRESH_TOKEN_TTL_DAYS),
        )
    )
    session.flush()
    return raw


def _load_refresh_token(session: Session, raw: str) -> RefreshToken:
    token = session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw))
    ).scalar_one_or_none()
    if token is None:
        raise SehatyForbiddenError("invalid refresh token")
    return token


def rotate_refresh_token(session: Session, raw: str) -> str:
    """Validate ``raw``, revoke it, and issue a fresh token (returned RAW).

    Rotation means a refresh token is single-use: replaying an old one after
    rotation fails, which surfaces token theft.
    """
    token = _load_refresh_token(session, raw)
    if token.revoked_at is not None:
        raise SehatyForbiddenError("refresh token revoked")
    if _aware(token.expires_at) <= _utcnow():
        raise SehatyForbiddenError("refresh token expired")
    token.revoked_at = _utcnow()
    session.flush()
    return issue_refresh_token(session, token.user_id, token.user_agent)


def revoke_refresh_token(session: Session, raw: str) -> None:
    """Revoke a single refresh token. Idempotent (unknown token is a no-op)."""
    token = session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw))
    ).scalar_one_or_none()
    if token is not None and token.revoked_at is None:
        token.revoked_at = _utcnow()
        session.flush()


def revoke_all(session: Session, user_id: int) -> None:
    """Revoke every active refresh token for a user (global logout)."""
    session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_utcnow())
    )
    session.flush()


# --------------------------------------------------------------------------- #
# Phone OTP
# --------------------------------------------------------------------------- #
def generate_otp(session: Session, phone: str) -> str:
    """Create a 6-digit OTP for ``phone`` and return the plaintext code.

    Only ``sha256(code)`` is stored (5-minute TTL). The returned code is for
    the SMS sender only and must never be logged.
    """
    code = f"{secrets.randbelow(1_000_000):06d}"
    session.add(
        PhoneOtp(
            phone=phone,
            code_hash=hash_token(code),
            expires_at=_utcnow() + timedelta(minutes=OTP_TTL_MINUTES),
            attempts=0,
        )
    )
    session.flush()
    return code


def verify_otp(session: Session, phone: str, code: str) -> PhoneOtp:
    """Verify a phone OTP, consuming it on success.

    Checks the latest unconsumed OTP for the phone: not expired, under the
    attempt cap, and matching hash. A wrong code increments ``attempts``;
    once the cap is reached the OTP is locked out.
    """
    otp = (
        session.execute(
            select(PhoneOtp)
            .where(PhoneOtp.phone == phone, PhoneOtp.consumed_at.is_(None))
            .order_by(PhoneOtp.id.desc())
        )
        .scalars()
        .first()
    )
    if otp is None:
        raise SehatyValidationError("no otp requested for this phone")
    if _aware(otp.expires_at) <= _utcnow():
        raise SehatyValidationError("otp expired")
    if otp.attempts >= OTP_MAX_ATTEMPTS:
        raise SehatyForbiddenError("otp locked: too many attempts")
    if otp.code_hash != hash_token(code):
        otp.attempts += 1
        # Commit the increment on its own so the attempt count survives even
        # when the caller rolls back the failed request — otherwise the
        # lockout could never accumulate across retries.
        session.commit()
        raise SehatyValidationError("invalid otp code")
    otp.consumed_at = _utcnow()
    session.flush()
    return otp
