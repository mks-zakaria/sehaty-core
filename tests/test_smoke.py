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
    assert sehaty.core.__version__ == "0.0.0"
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
