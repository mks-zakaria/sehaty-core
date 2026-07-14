"""Sehaty business-logic library.

Controllers (class-as-namespace with @staticmethod) validate inputs and
compose service calls. Services own SQLAlchemy IO against `sehaty.db`.
Failures raise the `SehatyError` taxonomy. No HTTP, no FastAPI.
"""

from sehaty.core._version import __version__
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyError,
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)

__all__ = [
    "__version__",
    "SehatyError",
    "SehatyNotFoundError",
    "SehatyValidationError",
    "SehatyForbiddenError",
    "SehatyConflictError",
]
