"""In-app notifications business logic (bell feed, unread count, mark read).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A
``Notification`` is an unread in-app message addressed to a single recipient
``user_id`` (e.g. "appointment confirmed", "new review", "payment recorded",
"referral rewarded"). ``notify`` is the internal API sibling controllers call to
drop a message into a user's feed; the read-side (``list_for``,
``unread_count``) powers the bell, and ``mark_read`` / ``mark_all_read`` clear
it. The recipient owns their feed: marking another user's notification read is a
``SehatyForbiddenError``. All IO flows through ``get_session``; failures raise the
``SehatyError`` taxonomy and methods never return ``None`` to signal an error.

The ``Notification`` row carries a single free-text ``message`` (there is no
separate title/body in the schema), a short ``kind`` tag, and an optional
``entity`` / ``entity_id`` pointer back to whatever triggered it.
"""

from sehaty.db import Notification
from sqlalchemy import func, select, update

from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyForbiddenError, SehatyNotFoundError, SehatyValidationError


class NotificationController:
    @staticmethod
    def notify(
        user_id: int,
        kind: str,
        message: str,
        entity: str | None = None,
        entity_id: int | None = None,
    ) -> Notification:
        """Create an unread notification in ``user_id``'s feed and return it.

        This is the internal API other controllers call after a domain event
        (booking confirmed, review published, cash payment recorded, referral
        rewarded). ``kind`` is a short machine tag and ``message`` the human
        text; both are required (blank → ``SehatyValidationError``).
        ``entity`` / ``entity_id`` optionally point back at the source row.
        """
        if not kind or not kind.strip():
            raise SehatyValidationError("kind must not be empty")
        if not message or not message.strip():
            raise SehatyValidationError("message must not be empty")

        with get_session() as session:
            notification = Notification(
                user_id=user_id,
                kind=kind,
                message=message,
                entity=entity,
                entity_id=entity_id,
                is_read=False,
            )
            session.add(notification)
            session.flush()
            return notification

    @staticmethod
    def list_for(
        user_id: int,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[Notification]:
        """Return a user's notifications, newest first (bell feed).

        When ``unread_only`` is set, only unread rows are returned. ``limit``
        caps the page size (must be positive → ``SehatyValidationError``).
        """
        if limit <= 0:
            raise SehatyValidationError("limit must be positive")

        stmt = select(Notification).where(Notification.user_id == user_id)
        if unread_only:
            stmt = stmt.where(Notification.is_read.is_(False))
        stmt = stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)

        with get_session() as session:
            return list(session.execute(stmt).scalars().all())

    @staticmethod
    def unread_count(user_id: int) -> int:
        """Return the number of unread notifications for the bell badge."""
        stmt = select(func.count(Notification.id)).where(
            Notification.user_id == user_id,
            Notification.is_read.is_(False),
        )
        with get_session() as session:
            return int(session.execute(stmt).scalar_one())

    @staticmethod
    def mark_read(user_id: int, notification_id: int) -> Notification:
        """Mark one of the user's own notifications read (idempotent).

        A missing id is ``SehatyNotFoundError``; a notification owned by another
        user is ``SehatyForbiddenError`` (the recipient owns their feed).
        Marking an already-read notification is a no-op and returns it.
        """
        with get_session() as session:
            notification = session.get(Notification, notification_id)
            if notification is None:
                raise SehatyNotFoundError(f"no notification {notification_id}")
            if notification.user_id != user_id:
                raise SehatyForbiddenError("cannot mark another user's notification read")

            if not notification.is_read:
                notification.is_read = True
                session.flush()
            return notification

    @staticmethod
    def mark_all_read(user_id: int) -> int:
        """Mark every unread notification for the user read; return the count."""
        stmt = (
            update(Notification)
            .where(Notification.user_id == user_id, Notification.is_read.is_(False))
            .values(is_read=True)
        )
        with get_session() as session:
            result = session.execute(stmt)
            return int(result.rowcount)
