"""Assistant membership + acting-doctor resolution tests (SQLite).

Covers the doctor onboarding a secretary (``create_assistant_account``),
linking/reactivating an existing ASSISTANT user (``link_existing``), the
membership listings and soft removal, and — the heart of the feature — the
``resolve_doctor_id`` helper that maps an actor (DOCTOR or ASSISTANT) onto the
effective doctor whose workspace they operate on.

``list_doctors_for_assistant`` outer-joins ``DoctorProfile`` for the doctor's
display name, and that table carries the PostGIS ``Geography`` column stock
SQLite cannot build, so (as in ``test_admin_lists.py``) this module registers a
dialect-scoped compilation shim (geo type -> ``TEXT``) for the test engine. The
reads use column-only projections, so ``geopoint`` itself is never selected.
"""

import pytest
from geoalchemy2 import Geography
from sehaty.db import DoctorAssistant, DoctorProfile, User, UserRole
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.assistants import AssistantController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import (
    SehatyConflictError,
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)


@compiles(Geography, "sqlite")
def _geography_as_text_on_sqlite(element, compiler, **kw) -> str:  # noqa: ANN001
    # SQLite has no geography type; store the column as opaque TEXT for tests.
    return "TEXT"


_TABLES = [User.__table__, DoctorAssistant.__table__, DoctorProfile.__table__]


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


def _seed_user(factory, *, email: str, role: UserRole, phone=None) -> int:
    with factory() as s:
        user = User(email=email, role=role, is_active=True, phone=phone)
        s.add(user)
        s.commit()
        return user.id


def _seed_doctor(factory, email="doc@clinic.ma") -> int:
    return _seed_user(factory, email=email, role=UserRole.DOCTOR)


def _seed_assistant(factory, email="asst@clinic.ma") -> int:
    return _seed_user(factory, email=email, role=UserRole.ASSISTANT)


# --------------------------------------------------------------------------- #
# create_assistant_account
# --------------------------------------------------------------------------- #


def test_create_assistant_account_creates_user_and_membership(db) -> None:
    doc = _seed_doctor(db)
    row = AssistantController.create_assistant_account(
        doc, email="sec@clinic.ma", phone="0600", full_name="Sara", password="s3cret"
    )
    assert row.id is not None
    assert row.email == "sec@clinic.ma"
    assert row.phone == "0600"
    assert row.full_name == "Sara"
    assert row.is_active is True

    with db() as s:
        user = s.get(User, row.id)
        assert user.role == UserRole.ASSISTANT
        assert user.password_hash and user.password_hash != "s3cret"
        link = s.execute(
            select(DoctorAssistant).where(
                DoctorAssistant.doctor_id == doc, DoctorAssistant.assistant_id == row.id
            )
        ).scalar_one()
        assert link.is_active is True


def test_create_assistant_account_duplicate_email_conflict(db) -> None:
    doc = _seed_doctor(db)
    AssistantController.create_assistant_account(doc, email="dup@clinic.ma", password="pw")
    with pytest.raises(SehatyConflictError):
        AssistantController.create_assistant_account(doc, email="dup@clinic.ma", password="pw")


def test_create_assistant_account_empty_password_validation(db) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        AssistantController.create_assistant_account(doc, email="x@clinic.ma", password="")


def test_create_assistant_account_bad_email_validation(db) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        AssistantController.create_assistant_account(doc, email="not-an-email", password="pw")


# --------------------------------------------------------------------------- #
# link_existing
# --------------------------------------------------------------------------- #


def test_link_existing_links_assistant(db) -> None:
    doc = _seed_doctor(db)
    asst = _seed_assistant(db)
    m = AssistantController.link_existing(doc, asst)
    assert m.doctor_id == doc
    assert m.assistant_id == asst
    assert m.is_active is True
    assert [a.id for a in AssistantController.list_assistants(doc)] == [asst]


def test_link_existing_non_assistant_validation(db) -> None:
    doc = _seed_doctor(db)
    patient = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    with pytest.raises(SehatyValidationError):
        AssistantController.link_existing(doc, patient)


def test_link_existing_missing_user_validation(db) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        AssistantController.link_existing(doc, 999999)


def test_link_existing_idempotent_and_reactivates(db) -> None:
    doc = _seed_doctor(db)
    asst = _seed_assistant(db)
    AssistantController.link_existing(doc, asst)
    AssistantController.remove_assistant(doc, asst)
    # Re-link the same pair: no duplicate row, reactivated.
    m = AssistantController.link_existing(doc, asst)
    assert m.is_active is True
    with db() as s:
        rows = (
            s.execute(
                select(DoctorAssistant).where(
                    DoctorAssistant.doctor_id == doc, DoctorAssistant.assistant_id == asst
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1  # idempotent per pair


# --------------------------------------------------------------------------- #
# list_assistants / list_doctors_for_assistant / remove_assistant
# --------------------------------------------------------------------------- #


def test_list_assistants_reflects_active_memberships(db) -> None:
    doc = _seed_doctor(db)
    a1 = _seed_assistant(db, email="a1@clinic.ma")
    a2 = _seed_assistant(db, email="a2@clinic.ma")
    AssistantController.link_existing(doc, a1)
    AssistantController.link_existing(doc, a2)
    assert sorted(a.id for a in AssistantController.list_assistants(doc)) == sorted([a1, a2])
    emails = {a.email for a in AssistantController.list_assistants(doc)}
    assert emails == {"a1@clinic.ma", "a2@clinic.ma"}


def test_list_doctors_for_assistant_reflects_memberships(db) -> None:
    d1 = _seed_doctor(db, email="d1@clinic.ma")
    d2 = _seed_doctor(db, email="d2@clinic.ma")
    asst = _seed_assistant(db)
    # Give d1 a profile so the name join is exercised; d2 has none (name None).
    with db() as s:
        s.add(DoctorProfile(user_id=d1, full_name="Dr Alaoui", slug="dr-alaoui", license_no="L1"))
        s.commit()
    AssistantController.link_existing(d1, asst)
    AssistantController.link_existing(d2, asst)
    refs = AssistantController.list_doctors_for_assistant(asst)
    assert sorted(d.doctor_id for d in refs) == sorted([d1, d2])
    names = {d.doctor_id: d.full_name for d in refs}
    assert names[d1] == "Dr Alaoui"
    assert names[d2] is None
    # The slug rides along from the profile (None when the doctor has none).
    slugs = {d.doctor_id: d.slug for d in refs}
    assert slugs[d1] == "dr-alaoui"
    assert slugs[d2] is None


def test_remove_assistant_deactivates(db) -> None:
    doc = _seed_doctor(db)
    asst = _seed_assistant(db)
    AssistantController.link_existing(doc, asst)
    AssistantController.remove_assistant(doc, asst)
    assert AssistantController.list_assistants(doc) == []
    assert AssistantController.list_doctors_for_assistant(asst) == []


def test_remove_assistant_not_found(db) -> None:
    doc = _seed_doctor(db)
    asst = _seed_assistant(db)
    with pytest.raises(SehatyNotFoundError):
        AssistantController.remove_assistant(doc, asst)


# --------------------------------------------------------------------------- #
# resolve_doctor_id
# --------------------------------------------------------------------------- #


def test_resolve_doctor_as_doctor_returns_self(db) -> None:
    doc = _seed_doctor(db)
    # requested is ignored for a doctor.
    assert AssistantController.resolve_doctor_id(doc, UserRole.DOCTOR) == doc
    assert AssistantController.resolve_doctor_id(doc, UserRole.DOCTOR, 12345) == doc


def test_resolve_assistant_single_doctor_no_request(db) -> None:
    doc = _seed_doctor(db)
    asst = _seed_assistant(db)
    AssistantController.link_existing(doc, asst)
    assert AssistantController.resolve_doctor_id(asst, UserRole.ASSISTANT) == doc


def test_resolve_assistant_requested_linked_ok(db) -> None:
    d1 = _seed_doctor(db, email="d1@clinic.ma")
    d2 = _seed_doctor(db, email="d2@clinic.ma")
    asst = _seed_assistant(db)
    AssistantController.link_existing(d1, asst)
    AssistantController.link_existing(d2, asst)
    assert AssistantController.resolve_doctor_id(asst, UserRole.ASSISTANT, d2) == d2


def test_resolve_assistant_requested_unlinked_forbidden(db) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    asst = _seed_assistant(db)
    AssistantController.link_existing(doc, asst)
    with pytest.raises(SehatyForbiddenError):
        AssistantController.resolve_doctor_id(asst, UserRole.ASSISTANT, other)


def test_resolve_assistant_multiple_no_request_validation(db) -> None:
    d1 = _seed_doctor(db, email="d1@clinic.ma")
    d2 = _seed_doctor(db, email="d2@clinic.ma")
    asst = _seed_assistant(db)
    AssistantController.link_existing(d1, asst)
    AssistantController.link_existing(d2, asst)
    with pytest.raises(SehatyValidationError):
        AssistantController.resolve_doctor_id(asst, UserRole.ASSISTANT)


def test_resolve_assistant_no_doctor_forbidden(db) -> None:
    asst = _seed_assistant(db)
    with pytest.raises(SehatyForbiddenError):
        AssistantController.resolve_doctor_id(asst, UserRole.ASSISTANT)


def test_resolve_assistant_deactivated_membership_not_counted(db) -> None:
    doc = _seed_doctor(db)
    asst = _seed_assistant(db)
    AssistantController.link_existing(doc, asst)
    AssistantController.remove_assistant(doc, asst)
    # No active membership left -> Forbidden (both requested and unrequested).
    with pytest.raises(SehatyForbiddenError):
        AssistantController.resolve_doctor_id(asst, UserRole.ASSISTANT)
    with pytest.raises(SehatyForbiddenError):
        AssistantController.resolve_doctor_id(asst, UserRole.ASSISTANT, doc)


def test_resolve_patient_forbidden(db) -> None:
    pat = _seed_user(db, email="pat@clinic.ma", role=UserRole.PATIENT)
    with pytest.raises(SehatyForbiddenError):
        AssistantController.resolve_doctor_id(pat, UserRole.PATIENT)
