"""Patient<->clinic messaging (1:1 threads + read watermarks).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). Every
conversation is a single :class:`MessageThread` keyed on the (patient, doctor)
pair; the *clinic side* of a thread is reachable by BOTH the doctor and any
ASSISTANT who actively works for that doctor (see
:meth:`AssistantController.resolve_doctor_id`). A message's ``sender_id`` is the
real author (patient, doctor, or assistant), but for unread accounting only the
*side* matters: any clinic-side sender counts as read-by-clinic.

Reads return small frozen dataclass projections (never ORM objects), so callers
hold detached, serialisable data. All IO goes through :func:`get_session`;
failures raise the :class:`SehatyError` taxonomy — methods never return ``None``
to signal an error.

Unread semantics: a message counts as unread for a side when its ``created_at``
is strictly after that side's read watermark (or the watermark is NULL) AND its
sender is not on that side. Clinic side "not on our side" == the sender is the
patient; patient side "not on our side" == the sender is not the patient. Posting
a message advances the *sender's* own watermark (you have read up to what you just
wrote); opening a thread advances the *viewer's* side watermark to now.
"""

from dataclasses import dataclass
from datetime import datetime

from sehaty.db import (
    DoctorAssistant,
    DoctorProfile,
    Message,
    MessageThread,
    PatientProfile,
    User,
    UserRole,
)
from sehaty.db.base import utcnow
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)

# Reject bodies longer than this (characters) — a sane ceiling for a chat turn.
_MAX_BODY = 4000
# Inbox preview is a single-line truncation of the latest message.
_PREVIEW_LEN = 140


@dataclass(frozen=True)
class MessageView:
    """One message, detached; ``mine`` is relative to the requesting user."""

    id: int
    thread_id: int
    sender_id: int
    body: str
    created_at: datetime
    mine: bool


@dataclass(frozen=True)
class ThreadView:
    """An inbox row: the thread plus display names, unread count and preview.

    ``unread`` and ``last_message_preview`` are computed for the side that asked
    (``0`` / the raw latest message when no side is implied, e.g. on create).
    """

    id: int
    patient_id: int
    doctor_id: int
    doctor_name: str | None
    patient_name: str | None
    last_message_at: datetime | None
    unread: int
    last_message_preview: str | None


@dataclass(frozen=True)
class ThreadDetail:
    """A full conversation: the thread header + its messages oldest->newest."""

    thread: ThreadView
    messages: list[MessageView]


def _preview(body: str | None) -> str | None:
    """Collapse a message body to a single trimmed inbox preview line."""
    if body is None:
        return None
    line = " ".join(body.split())
    if len(line) <= _PREVIEW_LEN:
        return line
    return line[: _PREVIEW_LEN - 1].rstrip() + "…"


class MessagingController:
    # ------------------------------------------------------------------ #
    # Role / participation resolution
    # ------------------------------------------------------------------ #
    @staticmethod
    def _require_role(session: Session, user_id: int, role: UserRole) -> None:
        """Assert ``user_id`` exists and carries ``role`` (else Not-Found/Validation)."""
        actual = session.execute(select(User.role).where(User.id == user_id)).scalar_one_or_none()
        if actual is None:
            raise SehatyNotFoundError(f"no user {user_id}")
        if actual != role:
            raise SehatyValidationError(f"user {user_id} is not a {role.value}")

    @staticmethod
    def _is_clinic_side(session: Session, user_id: int, doctor_id: int) -> bool:
        """True if ``user_id`` is the doctor or an ACTIVE assistant for that doctor."""
        if user_id == doctor_id:
            return True
        link = session.execute(
            select(DoctorAssistant.doctor_id).where(
                DoctorAssistant.assistant_id == user_id,
                DoctorAssistant.doctor_id == doctor_id,
                DoctorAssistant.is_active.is_(True),
            )
        ).scalar_one_or_none()
        return link is not None

    @staticmethod
    def _side_for(session: Session, thread: MessageThread, user_id: int) -> str:
        """Return ``"patient"`` / ``"doctor"`` for a participant, else forbidden."""
        if user_id == thread.patient_id:
            return "patient"
        if MessagingController._is_clinic_side(session, user_id, thread.doctor_id):
            return "doctor"
        raise SehatyForbiddenError("not a participant of this thread")

    # ------------------------------------------------------------------ #
    # View builders
    # ------------------------------------------------------------------ #
    @staticmethod
    def _unread_count(session: Session, thread: MessageThread, side: str) -> int:
        """Count messages unread for ``side`` (strictly after its watermark)."""
        if side == "patient":
            watermark = thread.patient_last_read_at
            sender_cond = Message.sender_id != thread.patient_id
        else:  # clinic side: only the patient's messages are unread-for-clinic
            watermark = thread.doctor_last_read_at
            sender_cond = Message.sender_id == thread.patient_id
        stmt = select(func.count(Message.id)).where(Message.thread_id == thread.id, sender_cond)
        if watermark is not None:
            stmt = stmt.where(Message.created_at > watermark)
        return int(session.execute(stmt).scalar_one())

    @staticmethod
    def _thread_view(session: Session, thread: MessageThread, side: str | None) -> ThreadView:
        """Project a thread to a :class:`ThreadView` for ``side`` (names + unread + preview)."""
        doctor_name = session.execute(
            select(DoctorProfile.full_name).where(DoctorProfile.user_id == thread.doctor_id)
        ).scalar_one_or_none()
        patient_name = session.execute(
            select(PatientProfile.full_name).where(PatientProfile.user_id == thread.patient_id)
        ).scalar_one_or_none()
        last_body = session.execute(
            select(Message.body)
            .where(Message.thread_id == thread.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        unread = MessagingController._unread_count(session, thread, side) if side else 0
        return ThreadView(
            id=thread.id,
            patient_id=thread.patient_id,
            doctor_id=thread.doctor_id,
            doctor_name=doctor_name,
            patient_name=patient_name,
            last_message_at=thread.last_message_at,
            unread=unread,
            last_message_preview=_preview(last_body),
        )

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    @staticmethod
    def start_or_get_thread(patient_id: int, doctor_id: int) -> ThreadView:
        """Return the (patient, doctor) thread, creating it if absent (idempotent).

        Both users must exist and carry the right role (``patient_id`` is a
        PATIENT, ``doctor_id`` a DOCTOR), else :class:`SehatyNotFoundError` /
        :class:`SehatyValidationError`. The ``uq_message_threads_patient_doctor``
        constraint makes the create race-safe: a concurrent insert that beats us
        raises :class:`IntegrityError`, which we swallow and re-query.
        """
        with get_session() as session:
            MessagingController._require_role(session, patient_id, UserRole.PATIENT)
            MessagingController._require_role(session, doctor_id, UserRole.DOCTOR)

            existing = session.execute(
                select(MessageThread).where(
                    MessageThread.patient_id == patient_id,
                    MessageThread.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return MessagingController._thread_view(session, existing, None)

            thread = MessageThread(patient_id=patient_id, doctor_id=doctor_id)
            session.add(thread)
            try:
                session.flush()
            except IntegrityError:
                # Lost the create race — the winning row is already committed.
                session.rollback()
                thread = session.execute(
                    select(MessageThread).where(
                        MessageThread.patient_id == patient_id,
                        MessageThread.doctor_id == doctor_id,
                    )
                ).scalar_one()
            return MessagingController._thread_view(session, thread, None)

    @staticmethod
    def post_message(sender_id: int, thread_id: int, body: str) -> MessageView:
        """Append a message to a thread the sender participates in.

        ``sender_id`` must be the thread's patient, the thread's doctor, or an
        active assistant for that doctor (else :class:`SehatyForbiddenError`).
        ``body`` is trimmed; empty/whitespace-only or longer than ``_MAX_BODY``
        is :class:`SehatyValidationError`. Bumps ``last_message_at`` and advances
        the sender's own read watermark (you have read what you just wrote), and
        returns the message with ``mine=True``.
        """
        text = body.strip()
        if not text:
            raise SehatyValidationError("message body must not be empty")
        if len(text) > _MAX_BODY:
            raise SehatyValidationError(f"message body exceeds {_MAX_BODY} characters")

        with get_session() as session:
            thread = session.get(MessageThread, thread_id)
            if thread is None:
                raise SehatyNotFoundError(f"no thread {thread_id}")
            side = MessagingController._side_for(session, thread, sender_id)

            now = utcnow()
            message = Message(
                thread_id=thread.id,
                sender_id=sender_id,
                body=text,
                created_at=now,
            )
            session.add(message)
            thread.last_message_at = now
            # The sender has, by definition, read up to their own message.
            if side == "patient":
                thread.patient_last_read_at = now
            else:
                thread.doctor_last_read_at = now
            session.flush()
            return MessageView(
                id=message.id,
                thread_id=message.thread_id,
                sender_id=message.sender_id,
                body=message.body,
                created_at=message.created_at,
                mine=True,
            )

    @staticmethod
    def list_threads_for_patient(patient_id: int) -> list[ThreadView]:
        """The patient's inbox, newest-activity first, with per-thread unread."""
        with get_session() as session:
            threads = list(
                session.execute(
                    select(MessageThread)
                    .where(MessageThread.patient_id == patient_id)
                    .order_by(
                        MessageThread.last_message_at.desc().nullslast(),
                        MessageThread.id.desc(),
                    )
                ).scalars()
            )
            return [
                MessagingController._thread_view(session, thread, "patient") for thread in threads
            ]

    @staticmethod
    def list_threads_for_doctor(doctor_id: int) -> list[ThreadView]:
        """The clinic inbox for a RESOLVED doctor, newest-activity first, with unread.

        ``doctor_id`` is the effective doctor id — the API resolves an assistant
        onto the doctor they act for (:meth:`AssistantController.resolve_doctor_id`)
        before calling. Unread counts the patient's messages after the clinic
        watermark.
        """
        with get_session() as session:
            threads = list(
                session.execute(
                    select(MessageThread)
                    .where(MessageThread.doctor_id == doctor_id)
                    .order_by(
                        MessageThread.last_message_at.desc().nullslast(),
                        MessageThread.id.desc(),
                    )
                ).scalars()
            )
            return [
                MessagingController._thread_view(session, thread, "doctor") for thread in threads
            ]

    @staticmethod
    def get_thread(user_id: int, thread_id: int) -> ThreadDetail:
        """Return a thread + its messages (oldest->newest) and MARK the viewer's side read.

        The caller must be a participant — the patient, the doctor, or an active
        assistant for the doctor (else :class:`SehatyForbiddenError`). Each
        message's ``mine`` is set for ``user_id``; the viewer's side read
        watermark is advanced to now so their unread count zeroes.
        """
        with get_session() as session:
            thread = session.get(MessageThread, thread_id)
            if thread is None:
                raise SehatyNotFoundError(f"no thread {thread_id}")
            side = MessagingController._side_for(session, thread, user_id)

            rows = list(
                session.execute(
                    select(Message)
                    .where(Message.thread_id == thread.id)
                    .order_by(Message.created_at.asc(), Message.id.asc())
                ).scalars()
            )
            messages = [
                MessageView(
                    id=m.id,
                    thread_id=m.thread_id,
                    sender_id=m.sender_id,
                    body=m.body,
                    created_at=m.created_at,
                    mine=(m.sender_id == user_id),
                )
                for m in rows
            ]

            # Mark read: advance the viewer's side watermark, then build the view
            # (so the returned unread reflects the just-read state == 0).
            now = utcnow()
            if side == "patient":
                thread.patient_last_read_at = now
            else:
                thread.doctor_last_read_at = now
            session.flush()

            view = MessagingController._thread_view(session, thread, side)
            return ThreadDetail(thread=view, messages=messages)

    @staticmethod
    def unread_total_for_patient(patient_id: int) -> int:
        """Total unread messages across all of a patient's threads (nav badge)."""
        with get_session() as session:
            threads = list(
                session.execute(
                    select(MessageThread).where(MessageThread.patient_id == patient_id)
                ).scalars()
            )
            return sum(
                MessagingController._unread_count(session, thread, "patient") for thread in threads
            )

    @staticmethod
    def unread_total_for_doctor(doctor_id: int) -> int:
        """Total unread messages across a (resolved) doctor's threads (nav badge)."""
        with get_session() as session:
            threads = list(
                session.execute(
                    select(MessageThread).where(MessageThread.doctor_id == doctor_id)
                ).scalars()
            )
            return sum(
                MessagingController._unread_count(session, thread, "doctor") for thread in threads
            )
