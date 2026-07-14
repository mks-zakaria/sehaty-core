"""Doctor business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern):
organized namespacing without forcing an instance-with-state. Validates
inputs, raises the SehatyError taxonomy, delegates IO to the service.
"""

from sehaty.db import DoctorProfile

from sehaty.core.errors import SehatyValidationError
from sehaty.core.services import doctors as doctor_service

_MAX_LIMIT = 100


class DoctorController:
    @staticmethod
    def search(
        *,
        city: str | None = None,
        query: str | None = None,
        limit: int = 20,
    ) -> list[DoctorProfile]:
        """Validate marketplace search inputs and delegate to the service."""
        if limit <= 0 or limit > _MAX_LIMIT:
            raise SehatyValidationError(f"limit out of range: {limit}")
        if query is not None and not query.strip():
            raise SehatyValidationError("query must not be blank")

        return doctor_service.search_doctors(
            city=city.strip() if city else None,
            query=query.strip() if query else None,
            limit=limit,
        )
