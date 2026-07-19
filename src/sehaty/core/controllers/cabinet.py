"""Cabinet (practice room) + cabinet-session business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A cabinet
is a solo doctor's practice room; a *session* is one open shift worked by the
owner or a substitute (a "friend in the field"). While a session ``is_open`` the
doctor is "online" at the cabinet and the secretary can check patients in against
it (see :meth:`AppointmentController.check_in`).

Reads/returns detached ``DomainModel`` projections (never ORM objects).
"""

from datetime import UTC, datetime

from sehaty.db import Cabinet, CabinetSession
from sqlalchemy import select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyConflictError, SehatyNotFoundError


class CabinetRow(DomainModel):
    id: int
    owner_doctor_id: int
    name: str
    address: str | None
    is_active: bool


class CabinetSessionRow(DomainModel):
    id: int
    cabinet_id: int
    acting_doctor_id: int
    opened_at: datetime
    closed_at: datetime | None
    is_open: bool


class CabinetController:
    @staticmethod
    def create(owner_doctor_id: int, name: str, address: str | None = None) -> CabinetRow:
        """Create a cabinet owned by ``owner_doctor_id`` and return it."""
        with get_session() as session:
            cabinet = Cabinet(owner_doctor_id=owner_doctor_id, name=name, address=address)
            session.add(cabinet)
            session.flush()
            return CabinetRow.model_validate(cabinet)

    @staticmethod
    def list_for_owner(owner_doctor_id: int) -> list[CabinetRow]:
        """List a doctor's own cabinets, newest first."""
        with get_session() as session:
            rows = session.execute(
                select(Cabinet)
                .where(Cabinet.owner_doctor_id == owner_doctor_id)
                .order_by(Cabinet.id.desc())
            ).scalars()
            return [CabinetRow.model_validate(c) for c in rows]

    @staticmethod
    def open_session(
        cabinet_id: int, acting_doctor_id: int, now: datetime | None = None
    ) -> CabinetSessionRow:
        """Open a shift at ``cabinet_id`` for ``acting_doctor_id`` (owner or substitute).

        The acting doctor is now "online" at the cabinet. Only one session may be
        open per cabinet at a time (else ``SehatyConflictError``); a substitute is
        simply a session whose ``acting_doctor_id`` differs from the cabinet owner.
        """
        now = _utcnow(now)
        with get_session() as session:
            cabinet = session.get(Cabinet, cabinet_id)
            if cabinet is None:
                raise SehatyNotFoundError(f"cabinet {cabinet_id} not found")
            existing = session.execute(
                select(CabinetSession.id).where(
                    CabinetSession.cabinet_id == cabinet_id,
                    CabinetSession.is_open.is_(True),
                )
            ).first()
            if existing is not None:
                raise SehatyConflictError("cabinet already has an open session")
            cabinet_session = CabinetSession(
                cabinet_id=cabinet_id,
                acting_doctor_id=acting_doctor_id,
                opened_at=now,
                is_open=True,
            )
            session.add(cabinet_session)
            session.flush()
            return CabinetSessionRow.model_validate(cabinet_session)

    @staticmethod
    def close_session(session_id: int, now: datetime | None = None) -> CabinetSessionRow:
        """Close an open shift (the doctor goes "offline")."""
        now = _utcnow(now)
        with get_session() as session:
            cabinet_session = session.get(CabinetSession, session_id)
            if cabinet_session is None:
                raise SehatyNotFoundError(f"cabinet session {session_id} not found")
            if cabinet_session.is_open:
                cabinet_session.is_open = False
                cabinet_session.closed_at = now
                session.flush()
            return CabinetSessionRow.model_validate(cabinet_session)

    @staticmethod
    def current_session(cabinet_id: int) -> CabinetSessionRow | None:
        """The cabinet's currently-open session, or ``None`` when nobody is online."""
        with get_session() as session:
            cabinet_session = session.execute(
                select(CabinetSession)
                .where(
                    CabinetSession.cabinet_id == cabinet_id,
                    CabinetSession.is_open.is_(True),
                )
                .order_by(CabinetSession.opened_at.desc())
            ).scalars().first()
            return (
                CabinetSessionRow.model_validate(cabinet_session)
                if cabinet_session is not None
                else None
            )


def _utcnow(now: datetime | None) -> datetime:
    """Normalise ``now`` to a UTC-aware instant (defaults to the current instant)."""
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)
