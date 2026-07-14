"""Auth/security configuration.

Values are read from the environment on import so the library stays
deployment-agnostic. Only non-secret tunables carry defaults; the JWT
signing secret falls back to an obviously-insecure placeholder so tests and
local dev run without setup — production MUST set ``SEHATY_JWT_SECRET``.
"""

import os

# JWT signing. HS256 keeps a single shared secret (no keypair to distribute);
# the future sehaty-api verifies with the same secret.
JWT_SECRET: str = os.environ.get("SEHATY_JWT_SECRET", "dev-insecure-secret-change-me")
JWT_ALGORITHM: str = "HS256"

# Access tokens are short-lived (<=15 min) so a leaked token expires fast;
# refresh tokens carry the long-lived session and are rotated on every use.
ACCESS_TOKEN_TTL_MINUTES: int = int(os.environ.get("SEHATY_ACCESS_TOKEN_TTL_MINUTES", "15"))
REFRESH_TOKEN_TTL_DAYS: int = int(os.environ.get("SEHATY_REFRESH_TOKEN_TTL_DAYS", "30"))

# Phone OTP: 6 digits, 5-minute TTL, locked after too many wrong attempts.
OTP_TTL_MINUTES: int = int(os.environ.get("SEHATY_OTP_TTL_MINUTES", "5"))
OTP_MAX_ATTEMPTS: int = int(os.environ.get("SEHATY_OTP_MAX_ATTEMPTS", "5"))

# bcrypt work factor. >=12 is the current OWASP floor.
BCRYPT_ROUNDS: int = int(os.environ.get("SEHATY_BCRYPT_ROUNDS", "12"))
