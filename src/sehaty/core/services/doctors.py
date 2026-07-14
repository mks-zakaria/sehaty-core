"""Doctor IO layer. The only place that knows about sessions and queries."""

from sehaty.db import DoctorProfile, VerificationStatus
from sqlalchemy import select

from sehaty.core.db.session import get_session


def search_doctors(
    *,
    city: str | None = None,
    query: str | None = None,
    limit: int = 20,
) -> list[DoctorProfile]:
    """Return verified doctor profiles matching the given filters.

    Only VERIFIED doctors are surfaced in the marketplace.
    """
    stmt = select(DoctorProfile).where(
        DoctorProfile.verification_status == VerificationStatus.VERIFIED
    )
    if city:
        stmt = stmt.where(DoctorProfile.city == city)
    if query:
        stmt = stmt.where(DoctorProfile.full_name.ilike(f"%{query}%"))
    stmt = stmt.limit(limit)

    with get_session() as session:
        return list(session.execute(stmt).scalars())
