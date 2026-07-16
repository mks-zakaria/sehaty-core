"""Messaging core tests on an in-memory SQLite engine.

Covers idempotent thread creation, posting from both sides, the clinic-side
authorization that lets a doctor's ASSISTANT act on their behalf, per-viewer
``mine`` flags, the read-watermark unread accounting (posting advances the
sender's side; opening a thread zeroes the viewer's side), and the body
validation guards. Only the tables this feature touches are created; ``DoctorProfile``
carries the PostGIS ``geopoint`` column that stock SQLite cannot compile, so a tiny
``Geography -> TEXT`` shim is registered for the ``sqlite`` dialect (messaging never
reads ``geopoint`` itself).
"""

import pytest
from geoalchemy2 import Geography
from sehaty.db import (
    DoctorAssistant,
    DoctorProfile,
    Message,
    MessageThread,
    PatientProfile,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, event
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.messaging import MessagingController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import (
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)


@compiles(Geography, "sqlite")
def _compile_geography_sqlite(type_, compiler, **kw) -> str:  # noqa: ANN001
    """Render the PostGIS ``geography`` column as TEXT so SQLite can build it.

    ``DoctorProfile`` carries a PostGIS ``geopoint`` column that stock SQLite
    cannot compile; messaging never touches it, so a TEXT stand-in is enough.
    """
    return "TEXT"


_TABLES = [
    User.__table__,
    PatientProfile.__table__,
    DoctorProfile.__table__,
    DoctorAssistant.__table__,
    MessageThread.__table__,
    Message.__table__,
]


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # GeoAlchemy2 wraps geopoint writes in ST_GeogFromText(); register a no-op
    # SQLite UDF so DoctorProfile inserts (with a NULL geopoint) round-trip.
    @event.listens_for(engine, "connect")
    def _register_geog_udf(dbapi_conn, _record) -> None:  # noqa: ANN001
        dbapi_conn.create_function("ST_GeogFromText", 1, lambda value: value)

    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _seed_user(factory: sessionmaker[Session], *, email: str, role: UserRole) -> int:
    with factory() as s:
        user = User(email=email, role=role, is_active=True)
        s.add(user)
        s.commit()
        return user.id


def _seed_patient(factory: sessionmaker[Session], *, email: str, name: str) -> int:
    uid = _seed_user(factory, email=email, role=UserRole.PATIENT)
    with factory() as s:
        s.add(PatientProfile(user_id=uid, full_name=name))
        s.commit()
    return uid


def _seed_doctor(factory: sessionmaker[Session], *, email: str, name: str, slug: str) -> int:
    uid = _seed_user(factory, email=email, role=UserRole.DOCTOR)
    with factory() as s:
        s.add(DoctorProfile(user_id=uid, full_name=name, slug=slug, license_no=slug))
        s.commit()
    return uid


def _seed_assistant(factory: sessionmaker[Session], *, email: str, doctor_id: int) -> int:
    uid = _seed_user(factory, email=email, role=UserRole.ASSISTANT)
    with factory() as s:
        s.add(DoctorAssistant(doctor_id=doctor_id, assistant_id=uid, is_active=True))
        s.commit()
    return uid


def _pair(factory: sessionmaker[Session]) -> tuple[int, int]:
    """Return (doctor_id, patient_id) with profiles."""
    doc = _seed_doctor(factory, email="doc@clinic.ma", name="Dr Amina", slug="dr-amina")
    pat = _seed_patient(factory, email="pat@clinic.ma", name="Youssef B.")
    return doc, pat


# --------------------------------------------------------------------------- #
# start_or_get_thread
# --------------------------------------------------------------------------- #


def test_start_or_get_is_idempotent(db: sessionmaker[Session]) -> None:
    doc, pat = _pair(db)
    t1 = MessagingController.start_or_get_thread(pat, doc)
    t2 = MessagingController.start_or_get_thread(pat, doc)
    assert t1.id == t2.id
    assert t1.doctor_name == "Dr Amina"
    assert t1.patient_name == "Youssef B."
    with db() as s:
        assert s.query(MessageThread).count() == 1


def test_start_or_get_validates_roles(db: sessionmaker[Session]) -> None:
    doc, pat = _pair(db)
    # patient/doctor swapped -> the "patient" arg is actually a doctor.
    with pytest.raises(SehatyValidationError):
        MessagingController.start_or_get_thread(doc, pat)


def test_start_or_get_missing_user_not_found(db: sessionmaker[Session]) -> None:
    doc, _ = _pair(db)
    with pytest.raises(SehatyNotFoundError):
        MessagingController.start_or_get_thread(999999, doc)


# --------------------------------------------------------------------------- #
# post_message + mine + read watermarks
# --------------------------------------------------------------------------- #


def test_post_and_view_flips_mine_and_marks_read(db: sessionmaker[Session]) -> None:
    doc, pat = _pair(db)
    thread = MessagingController.start_or_get_thread(pat, doc)

    m_pat = MessagingController.post_message(pat, thread.id, "  Bonjour docteur  ")
    assert m_pat.mine is True
    assert m_pat.body == "Bonjour docteur"  # trimmed

    m_doc = MessagingController.post_message(doc, thread.id, "Bonjour, comment puis-je aider ?")
    assert m_doc.mine is True

    # Doctor opens the thread: their message is mine, patient's is not.
    detail_doc = MessagingController.get_thread(doc, thread.id)
    by_id = {m.id: m for m in detail_doc.messages}
    assert [m.id for m in detail_doc.messages] == sorted(by_id)  # oldest -> newest
    assert by_id[m_doc.id].mine is True
    assert by_id[m_pat.id].mine is False
    assert detail_doc.thread.unread == 0  # get_thread marked the clinic side read

    # Patient opens the thread: the doctor's message is not theirs.
    detail_pat = MessagingController.get_thread(pat, thread.id)
    by_id = {m.id: m for m in detail_pat.messages}
    assert by_id[m_pat.id].mine is True
    assert by_id[m_doc.id].mine is False
    assert detail_pat.thread.unread == 0


def test_unread_counts_until_doctor_reads(db: sessionmaker[Session]) -> None:
    doc, pat = _pair(db)
    thread = MessagingController.start_or_get_thread(pat, doc)
    MessagingController.post_message(pat, thread.id, "j'ai une question")

    # Clinic inbox shows the unread patient message.
    inbox = MessagingController.list_threads_for_doctor(doc)
    assert len(inbox) == 1
    assert inbox[0].unread >= 1
    assert inbox[0].last_message_preview == "j'ai une question"
    assert MessagingController.unread_total_for_doctor(doc) >= 1
    # The patient (the sender) has nothing unread.
    assert MessagingController.unread_total_for_patient(pat) == 0

    # Doctor reads -> unread zeroes.
    MessagingController.get_thread(doc, thread.id)
    assert MessagingController.unread_total_for_doctor(doc) == 0
    assert MessagingController.list_threads_for_doctor(doc)[0].unread == 0


def test_patient_inbox_unread_from_doctor(db: sessionmaker[Session]) -> None:
    doc, pat = _pair(db)
    thread = MessagingController.start_or_get_thread(pat, doc)
    MessagingController.post_message(doc, thread.id, "vos résultats sont prêts")

    inbox = MessagingController.list_threads_for_patient(pat)
    assert len(inbox) == 1
    assert inbox[0].unread == 1
    assert inbox[0].doctor_name == "Dr Amina"
    MessagingController.get_thread(pat, thread.id)
    assert MessagingController.unread_total_for_patient(pat) == 0


# --------------------------------------------------------------------------- #
# Authorization (stranger vs acting assistant)
# --------------------------------------------------------------------------- #


def test_stranger_cannot_post_or_read(db: sessionmaker[Session]) -> None:
    doc, pat = _pair(db)
    thread = MessagingController.start_or_get_thread(pat, doc)
    stranger = _seed_user(db, email="stranger@clinic.ma", role=UserRole.PATIENT)

    with pytest.raises(SehatyForbiddenError):
        MessagingController.post_message(stranger, thread.id, "let me in")
    with pytest.raises(SehatyForbiddenError):
        MessagingController.get_thread(stranger, thread.id)


def test_assistant_acting_for_doctor_can_post_and_read(db: sessionmaker[Session]) -> None:
    doc, pat = _pair(db)
    assistant = _seed_assistant(db, email="sec@clinic.ma", doctor_id=doc)
    thread = MessagingController.start_or_get_thread(pat, doc)

    MessagingController.post_message(pat, thread.id, "je voudrais un rendez-vous")
    # The assistant reads on the clinic's behalf: clears the clinic unread.
    detail = MessagingController.get_thread(assistant, thread.id)
    assert detail.thread.unread == 0
    assert MessagingController.unread_total_for_doctor(doc) == 0

    # And the assistant can reply as the clinic side.
    reply = MessagingController.post_message(assistant, thread.id, "bien sûr, quelle date ?")
    assert reply.mine is True
    # The reply is unread for the patient (a clinic-side sender).
    assert MessagingController.unread_total_for_patient(pat) == 1


def test_inactive_assistant_is_forbidden(db: sessionmaker[Session]) -> None:
    doc, pat = _pair(db)
    assistant = _seed_assistant(db, email="ex@clinic.ma", doctor_id=doc)
    with db() as s:
        link = s.query(DoctorAssistant).filter_by(assistant_id=assistant).one()
        link.is_active = False
        s.commit()
    thread = MessagingController.start_or_get_thread(pat, doc)
    with pytest.raises(SehatyForbiddenError):
        MessagingController.post_message(assistant, thread.id, "still here?")


# --------------------------------------------------------------------------- #
# Body validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("body", ["", "   ", "\n\t  "])
def test_empty_body_rejected(db: sessionmaker[Session], body: str) -> None:
    doc, pat = _pair(db)
    thread = MessagingController.start_or_get_thread(pat, doc)
    with pytest.raises(SehatyValidationError):
        MessagingController.post_message(pat, thread.id, body)


def test_overlong_body_rejected(db: sessionmaker[Session]) -> None:
    doc, pat = _pair(db)
    thread = MessagingController.start_or_get_thread(pat, doc)
    with pytest.raises(SehatyValidationError):
        MessagingController.post_message(pat, thread.id, "x" * 4001)


def test_post_to_missing_thread_not_found(db: sessionmaker[Session]) -> None:
    doc, pat = _pair(db)
    with pytest.raises(SehatyNotFoundError):
        MessagingController.post_message(pat, 424242, "hello?")
