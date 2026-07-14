"""Import + wiring smoke test. No live DB required."""

import sehaty.db

import sehaty.core
from sehaty.core.controllers.doctors import DoctorController
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyError,
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)


def test_packages_import() -> None:
    # semantic-release rewrites _version.py, so assert a version is exposed
    # rather than pinning a literal that goes stale on every release.
    assert isinstance(sehaty.core.__version__, str) and sehaty.core.__version__
    assert hasattr(sehaty.db, "DoctorProfile")


def test_error_taxonomy() -> None:
    assert SehatyError.http_status == 500
    assert SehatyError.code == "sehaty_error"
    assert SehatyNotFoundError.http_status == 404
    assert SehatyValidationError.http_status == 400
    assert SehatyForbiddenError.http_status == 403
    assert SehatyConflictError.http_status == 409
    for exc in (
        SehatyNotFoundError,
        SehatyValidationError,
        SehatyForbiddenError,
        SehatyConflictError,
    ):
        assert issubclass(exc, SehatyError)


def test_controller_search_is_callable() -> None:
    assert callable(DoctorController.search)
