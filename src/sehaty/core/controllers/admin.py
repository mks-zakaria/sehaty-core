"""Accreditation business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern): an
admin verifies (accredits) or rejects (revokes) a doctor's licence, and every
such decision is written to the immutable ``AuditLog`` trail.

Reads use column-only selects so we never touch ``DoctorProfile.geopoint`` (a
full-entity load wraps the PostGIS column in ``AsBinary``, which stock SQLite
lacks — see the shims in the auth tests). Failures raise the ``SehatyError``
taxonomy; methods never return ``None`` to signal an error.
"""

from dataclasses import dataclass

from sehaty.db import AuditLog, DoctorProfile, User, VerificationStatus
from sqlalchemy import select, update

from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError


@dataclass(frozen=True)
class PendingProfessional:
    """A doctor awaiting accreditation (column-only projection, no geopoint)."""

    user_id: int
    full_name: str
    speciality: str | None
    license_no: str
    city: str | None
    email: str


class AdminController:
    @staticmethod
    def list_pending_professionals() -> list[PendingProfessional]:
        """Return every doctor whose profile is still ``PENDING``.

        Column-only projection joined to ``User`` for the email; ``geopoint`` is
        never selected. ``speciality`` has no column on ``DoctorProfile`` (it
        lives in the ``specialties`` catalogue, out of scope here) so it is left
        ``None`` for the caller to enrich.
        """
        stmt = (
            select(
                DoctorProfile.user_id,
                DoctorProfile.full_name,
                DoctorProfile.license_no,
                DoctorProfile.city,
                User.email,
            )
            .join(User, User.id == DoctorProfile.user_id)
            .where(DoctorProfile.verification_status == VerificationStatus.PENDING)
        )
        with get_session() as session:
            rows = session.execute(stmt).all()
        return [
            PendingProfessional(
                user_id=row.user_id,
                full_name=row.full_name,
                speciality=None,
                license_no=row.license_no,
                city=row.city,
                email=row.email,
            )
            for row in rows
        ]

    @staticmethod
    def accredit(admin_id: int, user_id: int) -> None:
        """Mark the doctor VERIFIED and record an ``ACCREDIT`` audit entry."""
        AdminController._set_status(admin_id, user_id, VerificationStatus.VERIFIED, "ACCREDIT")

    @staticmethod
    def revoke(admin_id: int, user_id: int) -> None:
        """Mark the doctor REJECTED and record a ``REVOKE`` audit entry."""
        AdminController._set_status(admin_id, user_id, VerificationStatus.REJECTED, "REVOKE")

    @staticmethod
    def is_doctor_verified(user_id: int) -> bool:
        """True iff the doctor's profile is VERIFIED.

        Reusable helper the API's ``require_verified`` dependency can call so
        the verification rule lives in one place.
        """
        stmt = select(DoctorProfile.verification_status).where(DoctorProfile.user_id == user_id)
        with get_session() as session:
            status = session.execute(stmt).scalar_one_or_none()
        return status == VerificationStatus.VERIFIED

    @staticmethod
    def _set_status(
        admin_id: int,
        user_id: int,
        status: VerificationStatus,
        action: str,
    ) -> None:
        """Flip a doctor's verification status and append an audit-log row.

        Uses a column-only existence probe + bulk ``UPDATE`` rather than loading
        the entity, so ``DoctorProfile.geopoint`` is never selected.
        """
        with get_session() as session:
            exists = session.execute(
                select(DoctorProfile.user_id).where(DoctorProfile.user_id == user_id)
            ).scalar_one_or_none()
            if exists is None:
                raise SehatyNotFoundError(f"no doctor profile for user {user_id}")
            session.execute(
                update(DoctorProfile)
                .where(DoctorProfile.user_id == user_id)
                .values(verification_status=status)
            )
            session.add(
                AuditLog(
                    actor_user_id=admin_id,
                    action=action,
                    entity="doctor_profile",
                    entity_id=user_id,
                )
            )
