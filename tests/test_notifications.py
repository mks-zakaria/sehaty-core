"""In-app notification core tests on an in-memory SQLite engine.

Covers the bell feed (create-unread, newest-first ordering, ``unread_only``
filter, ``limit``), the unread badge count, per-notification mark-read (flip +
idempotency + cross-user ownership guard) and the bulk mark-all-read. Only the
``users`` and ``notifications`` tables are created — neither carries the PostGIS
``geopoint`` column, so no dialect shims are needed.
"""

import pytest
from sehaty.db import Notification, User, UserRole
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.notifications import NotificationController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyForbiddenError, SehatyNotFoundError, SehatyValidationError

_TABLES = [
    User.__table__,
    Notification.__table__,
]


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _seed_user(factory: sessionmaker[Session], email: str) -> int:
    with factory() as s:
        user = User(email=email, role=UserRole.PATIENT, is_active=True)
        s.add(user)
        s.commit()
        return user.id


# --------------------------------------------------------------------------- #
# notify + feed
# --------------------------------------------------------------------------- #


def test_notify_creates_unread(db: sessionmaker[Session]) -> None:
    user_id = _seed_user(db, "pat@sehaty.ma")

    n = NotificationController.notify(
        user_id,
        kind="appointment",
        message="Your appointment is confirmed",
        entity="appointment",
        entity_id=42,
    )

    assert n.id is not None
    assert n.user_id == user_id
    assert n.kind == "appointment"
    assert n.message == "Your appointment is confirmed"
    assert n.entity == "appointment"
    assert n.entity_id == 42
    assert n.is_read is False


def test_notify_rejects_blank(db: sessionmaker[Session]) -> None:
    user_id = _seed_user(db, "pat@sehaty.ma")
    with pytest.raises(SehatyValidationError):
        NotificationController.notify(user_id, kind="", message="hi")
    with pytest.raises(SehatyValidationError):
        NotificationController.notify(user_id, kind="review", message="   ")


def test_list_for_is_newest_first(db: sessionmaker[Session]) -> None:
    user_id = _seed_user(db, "pat@sehaty.ma")
    first = NotificationController.notify(user_id, kind="a", message="first")
    second = NotificationController.notify(user_id, kind="b", message="second")
    third = NotificationController.notify(user_id, kind="c", message="third")

    feed = NotificationController.list_for(user_id)
    assert [n.id for n in feed] == [third.id, second.id, first.id]


def test_list_for_scopes_to_user(db: sessionmaker[Session]) -> None:
    alice = _seed_user(db, "alice@sehaty.ma")
    bob = _seed_user(db, "bob@sehaty.ma")
    NotificationController.notify(alice, kind="a", message="for alice")
    NotificationController.notify(bob, kind="b", message="for bob")

    assert [n.message for n in NotificationController.list_for(alice)] == ["for alice"]
    assert [n.message for n in NotificationController.list_for(bob)] == ["for bob"]


def test_list_for_unread_only_filters(db: sessionmaker[Session]) -> None:
    user_id = _seed_user(db, "pat@sehaty.ma")
    read_one = NotificationController.notify(user_id, kind="a", message="read me")
    NotificationController.notify(user_id, kind="b", message="still unread")
    NotificationController.mark_read(user_id, read_one.id)

    all_feed = NotificationController.list_for(user_id)
    unread_feed = NotificationController.list_for(user_id, unread_only=True)

    assert len(all_feed) == 2
    assert [n.message for n in unread_feed] == ["still unread"]


def test_list_for_respects_limit(db: sessionmaker[Session]) -> None:
    user_id = _seed_user(db, "pat@sehaty.ma")
    for i in range(5):
        NotificationController.notify(user_id, kind="k", message=f"n{i}")

    feed = NotificationController.list_for(user_id, limit=2)
    assert len(feed) == 2
    with pytest.raises(SehatyValidationError):
        NotificationController.list_for(user_id, limit=0)


# --------------------------------------------------------------------------- #
# unread_count
# --------------------------------------------------------------------------- #


def test_unread_count(db: sessionmaker[Session]) -> None:
    user_id = _seed_user(db, "pat@sehaty.ma")
    assert NotificationController.unread_count(user_id) == 0

    a = NotificationController.notify(user_id, kind="a", message="one")
    NotificationController.notify(user_id, kind="b", message="two")
    assert NotificationController.unread_count(user_id) == 2

    NotificationController.mark_read(user_id, a.id)
    assert NotificationController.unread_count(user_id) == 1


# --------------------------------------------------------------------------- #
# mark_read
# --------------------------------------------------------------------------- #


def test_mark_read_flips_and_is_idempotent(db: sessionmaker[Session]) -> None:
    user_id = _seed_user(db, "pat@sehaty.ma")
    n = NotificationController.notify(user_id, kind="a", message="hi")

    first = NotificationController.mark_read(user_id, n.id)
    assert first.is_read is True

    # Idempotent: marking again is a no-op and stays read.
    second = NotificationController.mark_read(user_id, n.id)
    assert second.is_read is True
    assert NotificationController.unread_count(user_id) == 0


def test_mark_read_unknown_id_raises(db: sessionmaker[Session]) -> None:
    user_id = _seed_user(db, "pat@sehaty.ma")
    with pytest.raises(SehatyNotFoundError):
        NotificationController.mark_read(user_id, 999)


def test_mark_read_rejects_other_users_notification(db: sessionmaker[Session]) -> None:
    alice = _seed_user(db, "alice@sehaty.ma")
    bob = _seed_user(db, "bob@sehaty.ma")
    n = NotificationController.notify(alice, kind="a", message="alice only")

    with pytest.raises(SehatyForbiddenError):
        NotificationController.mark_read(bob, n.id)

    # Alice's notification is untouched.
    assert NotificationController.unread_count(alice) == 1


# --------------------------------------------------------------------------- #
# mark_all_read
# --------------------------------------------------------------------------- #


def test_mark_all_read_zeroes_unread(db: sessionmaker[Session]) -> None:
    user_id = _seed_user(db, "pat@sehaty.ma")
    other = _seed_user(db, "other@sehaty.ma")
    NotificationController.notify(user_id, kind="a", message="one")
    NotificationController.notify(user_id, kind="b", message="two")
    NotificationController.notify(user_id, kind="c", message="three")
    NotificationController.notify(other, kind="x", message="other's")

    marked = NotificationController.mark_all_read(user_id)
    assert marked == 3
    assert NotificationController.unread_count(user_id) == 0

    # Only the caller's feed is cleared.
    assert NotificationController.unread_count(other) == 1

    # Idempotent: nothing left to mark.
    assert NotificationController.mark_all_read(user_id) == 0
