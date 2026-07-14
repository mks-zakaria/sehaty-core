"""Error taxonomy for Sehaty business logic.

Controllers raise these; the transport layer (a future sehaty-api) maps
`http_status` + `code` onto an HTTP response. Never return `None` to mean
"not found" — raise the right subclass instead.

Pattern: see senior-dev reference/api.md "Error taxonomy".
"""


class SehatyError(Exception):
    """Base for all known business errors."""

    http_status = 500
    code = "sehaty_error"


class SehatyNotFoundError(SehatyError):
    http_status = 404
    code = "not_found"


class SehatyValidationError(SehatyError):
    http_status = 400
    code = "validation_error"


class SehatyForbiddenError(SehatyError):
    http_status = 403
    code = "forbidden"


class SehatyConflictError(SehatyError):
    http_status = 409
    code = "conflict"
