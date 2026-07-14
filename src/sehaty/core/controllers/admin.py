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
from datetime import datetime

from sehaty.db import AuditLog, DoctorProfile, Plan, Subscription, User, VerificationStatus
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


@dataclass(frozen=True)
class UserRow:
    """One row of the admin Users page (column-only projection, no geopoint).

    ``full_name`` comes from the doctor's profile via an outer join and is
    ``None`` for patients/admins (who carry no ``DoctorProfile``).
    """

    id: int
    email: str
    phone: str | None
    role: str
    is_active: bool
    full_name: str | None
    created_at: datetime


@dataclass(frozen=True)
class SubscriptionRow:
    """One row of the admin Subscriptions page (detached, column-only).

    Joins the subscription to its ``Plan`` and (by ``doctor_id``) to the
    doctor's ``DoctorProfile`` for the display name; ``geopoint`` is never
    selected. ``doctor_name`` is ``None`` when the doctor has no profile row.
    """

    id: int
    doctor_id: int
    doctor_name: str | None
    plan_code: str
    plan_name: str
    price_month: float
    currency: str
    status: str
    current_period_end: datetime


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
    def list_users(
        role: str | None = None,
        is_active: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[UserRow]:
        """List platform users for the admin Users page, newest first.

        Column-only projection outer-joined to ``DoctorProfile`` for the
        display name (``geopoint`` is never selected), so doctors carry a
        ``full_name`` and patients/admins get ``None``. Optional ``role`` /
        ``is_active`` filters narrow the set. Ordered by ``created_at`` (then
        ``id``) descending for a stable newest-first page.
        """
        stmt = (
            select(
                User.id,
                User.email,
                User.phone,
                User.role,
                User.is_active,
                DoctorProfile.full_name,
                User.created_at,
            )
            .outerjoin(DoctorProfile, DoctorProfile.user_id == User.id)
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if role is not None:
            stmt = stmt.where(User.role == role)
        if is_active is not None:
            stmt = stmt.where(User.is_active.is_(is_active))
        with get_session() as session:
            rows = session.execute(stmt).all()
        return [
            UserRow(
                id=row.id,
                email=row.email,
                phone=row.phone,
                role=str(row.role),
                is_active=bool(row.is_active),
                full_name=row.full_name,
                created_at=row.created_at,
            )
            for row in rows
        ]

    @staticmethod
    def list_subscriptions(
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SubscriptionRow]:
        """List subscriptions for the admin Subscriptions page, newest first.

        Column-only projection joining ``Subscription`` to its ``Plan`` and
        outer-joining ``DoctorProfile`` (by ``doctor_id``) for the doctor's
        display name; ``geopoint`` is never selected. Optional ``status`` filter
        narrows the set. Ordered by ``id`` descending.
        """
        stmt = (
            select(
                Subscription.id,
                Subscription.doctor_id,
                DoctorProfile.full_name,
                Plan.code,
                Plan.name,
                Plan.price_month,
                Plan.currency,
                Subscription.status,
                Subscription.current_period_end,
            )
            .join(Plan, Plan.id == Subscription.plan_id)
            .outerjoin(DoctorProfile, DoctorProfile.user_id == Subscription.doctor_id)
            .order_by(Subscription.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if status is not None:
            stmt = stmt.where(Subscription.status == status)
        with get_session() as session:
            rows = session.execute(stmt).all()
        return [
            SubscriptionRow(
                id=row.id,
                doctor_id=row.doctor_id,
                doctor_name=row.full_name,
                plan_code=row.code,
                plan_name=row.name,
                price_month=float(row.price_month),
                currency=row.currency,
                status=str(row.status),
                current_period_end=row.current_period_end,
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
