"""Assistant membership + acting-doctor resolution (doctor_assistants).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A doctor
onboards a secretary/assistant (an ASSISTANT-role ``User`` with a login) who then
acts *within that doctor's workspace*: manage the schedule, the patient register,
call patients to confirm appointments, etc. One assistant can serve several
doctors and a doctor can have several assistants, so :class:`DoctorAssistant` is a
plain many-to-many link with a soft ``is_active`` toggle.

The pivotal helper is :meth:`AssistantController.resolve_doctor_id`: it maps the
authenticated actor (a DOCTOR acting on their own workspace, or an ASSISTANT
acting on a doctor's) onto the *effective* ``doctor_id``. The API layer calls it
once, then reuses the existing doctor-scoped controllers (patients, appointments,
prescriptions, ...) unchanged with the resolved id.

Reads return small frozen dataclass projections (never ORM objects), so callers
hold detached, serialisable data. All IO goes through :func:`get_session`;
failures raise the :class:`SehatyError` taxonomy — methods never return ``None``
to signal an error.
"""

from dataclasses import dataclass
from datetime import datetime

from sehaty.db import DoctorAssistant, DoctorProfile, User, UserRole
from sqlalchemy import select

from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)
from sehaty.core.security import hash_password


@dataclass(frozen=True)
class AssistantRow:
    """One assistant account (detached, column-only projection).

    ``id`` is the assistant's ``User`` id. ``full_name`` has no column on a plain
    ASSISTANT user, so it is only populated (as an echo of the caller-supplied
    value) by :meth:`create_assistant_account`; it is ``None`` when listing.
    """

    id: int
    email: str
    phone: str | None
    full_name: str | None
    is_active: bool
    created_at: datetime


@dataclass(frozen=True)
class MembershipRow:
    """One doctor<->assistant link row (detached projection)."""

    doctor_id: int
    assistant_id: int
    is_active: bool


@dataclass(frozen=True)
class DoctorRef:
    """A doctor an assistant works for (id + display name from the profile)."""

    doctor_id: int
    full_name: str | None


def _valid_email(email: str) -> bool:
    """Minimal structural email check: ``local@domain`` with no whitespace."""
    email = email.strip()
    if not email or " " in email or email.count("@") != 1:
        return False
    local, _, domain = email.partition("@")
    return bool(local) and bool(domain) and "." in domain


class AssistantController:
    @staticmethod
    def create_assistant_account(
        doctor_id: int,
        email: str,
        phone: str | None = None,
        full_name: str | None = None,
        password: str = "",
    ) -> AssistantRow:
        """Create an ASSISTANT ``User`` (with a login) and link it to the doctor.

        The doctor onboards their secretary: a new active ASSISTANT user is
        created with a bcrypt ``password_hash`` and linked to ``doctor_id`` via
        an active :class:`DoctorAssistant` row. ``email`` must be well-formed and
        ``password`` non-empty (else :class:`SehatyValidationError`); a duplicate
        email raises :class:`SehatyConflictError`. Returns the new
        :class:`AssistantRow` (``full_name`` echoes the supplied value).
        """
        if not _valid_email(email):
            raise SehatyValidationError("a valid email is required")
        if not password:
            raise SehatyValidationError("password must not be empty")
        email = email.strip()
        with get_session() as session:
            exists = session.execute(
                select(User.id).where(User.email == email)
            ).scalar_one_or_none()
            if exists is not None:
                raise SehatyConflictError("email already registered")
            user = User(
                email=email,
                phone=phone,
                password_hash=hash_password(password),
                role=UserRole.ASSISTANT,
                is_active=True,
            )
            session.add(user)
            session.flush()
            session.add(DoctorAssistant(doctor_id=doctor_id, assistant_id=user.id, is_active=True))
            session.flush()
            return AssistantRow(
                id=user.id,
                email=user.email,
                phone=user.phone,
                full_name=full_name,
                is_active=user.is_active,
                created_at=user.created_at,
            )

    @staticmethod
    def link_existing(doctor_id: int, assistant_user_id: int) -> MembershipRow:
        """Link an existing ASSISTANT user to this doctor (idempotent).

        The target user must exist and have role ASSISTANT (else
        :class:`SehatyValidationError`). Idempotent per (doctor, assistant) pair:
        an existing link is reactivated if it was previously deactivated, and a
        missing one is created active. Returns the :class:`MembershipRow`.
        """
        with get_session() as session:
            user = session.get(User, assistant_user_id)
            if user is None or user.role != UserRole.ASSISTANT:
                raise SehatyValidationError("target user is not an assistant")
            link = session.execute(
                select(DoctorAssistant).where(
                    DoctorAssistant.doctor_id == doctor_id,
                    DoctorAssistant.assistant_id == assistant_user_id,
                )
            ).scalar_one_or_none()
            if link is None:
                link = DoctorAssistant(
                    doctor_id=doctor_id, assistant_id=assistant_user_id, is_active=True
                )
                session.add(link)
            else:
                link.is_active = True
            session.flush()
            return MembershipRow(
                doctor_id=link.doctor_id,
                assistant_id=link.assistant_id,
                is_active=link.is_active,
            )

    @staticmethod
    def list_assistants(doctor_id: int) -> list[AssistantRow]:
        """List the doctor's ACTIVE assistants (joined to ``users`` for contact).

        ``full_name`` is ``None`` (a plain ASSISTANT user carries no name column).
        """
        stmt = (
            select(User.id, User.email, User.phone, User.is_active, User.created_at)
            .join(DoctorAssistant, DoctorAssistant.assistant_id == User.id)
            .where(
                DoctorAssistant.doctor_id == doctor_id,
                DoctorAssistant.is_active.is_(True),
            )
            .order_by(User.id.asc())
        )
        with get_session() as session:
            rows = session.execute(stmt).all()
        return [
            AssistantRow(
                id=row.id,
                email=row.email,
                phone=row.phone,
                full_name=None,
                is_active=bool(row.is_active),
                created_at=row.created_at,
            )
            for row in rows
        ]

    @staticmethod
    def list_doctors_for_assistant(assistant_id: int) -> list[DoctorRef]:
        """List the doctors this assistant ACTIVELY works for (id + name).

        Outer-joins :class:`DoctorProfile` (by ``doctor_id``) for the display
        name; ``geopoint`` is never selected. ``full_name`` is ``None`` if the
        doctor has no profile row.
        """
        stmt = (
            select(DoctorAssistant.doctor_id, DoctorProfile.full_name)
            .outerjoin(DoctorProfile, DoctorProfile.user_id == DoctorAssistant.doctor_id)
            .where(
                DoctorAssistant.assistant_id == assistant_id,
                DoctorAssistant.is_active.is_(True),
            )
            .order_by(DoctorAssistant.doctor_id.asc())
        )
        with get_session() as session:
            rows = session.execute(stmt).all()
        return [DoctorRef(doctor_id=row.doctor_id, full_name=row.full_name) for row in rows]

    @staticmethod
    def remove_assistant(doctor_id: int, assistant_id: int) -> None:
        """Deactivate a doctor<->assistant link (soft off).

        Raises :class:`SehatyNotFoundError` if no ACTIVE membership exists for the
        pair. History is preserved (``is_active`` is flipped, the row is kept).
        """
        with get_session() as session:
            link = session.execute(
                select(DoctorAssistant).where(
                    DoctorAssistant.doctor_id == doctor_id,
                    DoctorAssistant.assistant_id == assistant_id,
                    DoctorAssistant.is_active.is_(True),
                )
            ).scalar_one_or_none()
            if link is None:
                raise SehatyNotFoundError(
                    f"no active assistant {assistant_id} for doctor {doctor_id}"
                )
            link.is_active = False
            session.flush()

    @staticmethod
    def resolve_doctor_id(
        actor_user_id: int,
        actor_role: UserRole | str,
        requested_doctor_id: int | None = None,
    ) -> int:
        """Resolve the effective ``doctor_id`` whose workspace the actor operates on.

        The single entry point the API uses so an assistant can reuse the
        existing doctor-scoped controllers (patients, appointments,
        prescriptions, ...) with the resolved id. Rules:

        * DOCTOR -> ``actor_user_id`` (``requested_doctor_id`` is ignored).
        * ASSISTANT -> if ``requested_doctor_id`` is given, an ACTIVE membership
          (actor <-> requested) must exist, else :class:`SehatyForbiddenError`.
          If ``requested_doctor_id`` is ``None``: exactly ONE active doctor ->
          that doctor; several -> :class:`SehatyValidationError` ("specify a
          doctor"); none -> :class:`SehatyForbiddenError`.
        * any other role -> :class:`SehatyForbiddenError`.
        """
        role = UserRole(actor_role)
        if role == UserRole.DOCTOR:
            return actor_user_id
        if role != UserRole.ASSISTANT:
            raise SehatyForbiddenError("role may not act on a doctor workspace")

        with get_session() as session:
            if requested_doctor_id is not None:
                link = session.execute(
                    select(DoctorAssistant.doctor_id).where(
                        DoctorAssistant.assistant_id == actor_user_id,
                        DoctorAssistant.doctor_id == requested_doctor_id,
                        DoctorAssistant.is_active.is_(True),
                    )
                ).scalar_one_or_none()
                if link is None:
                    raise SehatyForbiddenError(
                        f"assistant {actor_user_id} does not work for doctor {requested_doctor_id}"
                    )
                return requested_doctor_id

            doctor_ids = (
                session.execute(
                    select(DoctorAssistant.doctor_id).where(
                        DoctorAssistant.assistant_id == actor_user_id,
                        DoctorAssistant.is_active.is_(True),
                    )
                )
                .scalars()
                .all()
            )
        if not doctor_ids:
            raise SehatyForbiddenError("assistant is not linked to any doctor")
        if len(doctor_ids) > 1:
            raise SehatyValidationError("specify a doctor")
        return doctor_ids[0]
