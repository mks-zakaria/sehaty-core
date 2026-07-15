"""Prescription-template CRUD tests on in-memory SQLite.

Covers create (persists name/notes/items, name and item validation), the
doctor-scoped list (cross-doctor exclusion, name ordering) and the
ownership-checked delete (foreign/missing -> NotFound). Only the tables these
features touch are created.
"""

import pytest
from sehaty.db import PrescriptionTemplate, User, UserRole
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.prescription_templates import PrescriptionTemplateController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError

_TABLES = [
    User.__table__,
    PrescriptionTemplate.__table__,
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


def _seed_doctor(factory, email: str = "doc@clinic.ma") -> int:
    with factory() as s:
        user = User(email=email, role=UserRole.DOCTOR, is_active=True)
        s.add(user)
        s.commit()
        return user.id


def _item(**over) -> dict:
    base = {"drug_name": "Doliprane", "dosage": "1 tablet", "frequency": "3x/day"}
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #


def test_create_persists_name_notes_items(db) -> None:
    doc = _seed_doctor(db)
    row = PrescriptionTemplateController.create(
        doc,
        name="Angine - adulte",
        items=[_item(duration_days=5, instructions="after meals"), _item(drug_name="Amoxicilline")],
        notes="rest well",
    )
    assert row.name == "Angine - adulte"
    assert row.notes == "rest well"
    assert row.created_at is not None
    assert len(row.items) == 2
    first = row.items[0]
    assert first.drug_name == "Doliprane"
    assert first.dosage == "1 tablet"
    assert first.frequency == "3x/day"
    assert first.duration_days == 5
    assert first.instructions == "after meals"
    assert row.items[1].drug_name == "Amoxicilline"
    assert row.items[1].duration_days is None

    # Persisted to the JSON column in the stored dict shape.
    with db() as s:
        stored = s.get(PrescriptionTemplate, row.id)
        assert stored.name == "Angine - adulte"
        assert stored.items[0]["drug_name"] == "Doliprane"
        assert stored.items[0]["duration_days"] == 5


def test_create_strips_and_normalizes_item(db) -> None:
    doc = _seed_doctor(db)
    row = PrescriptionTemplateController.create(
        doc,
        name="  Preset  ",
        items=[{"drug_name": "  Ibuprofene ", "dosage": " 200mg ", "frequency": " 2x/day "}],
    )
    assert row.name == "Preset"  # stripped
    assert row.items[0].drug_name == "Ibuprofene"
    assert row.items[0].dosage == "200mg"
    assert row.items[0].frequency == "2x/day"


def test_create_empty_name_raises(db) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        PrescriptionTemplateController.create(doc, name="   ", items=[_item()])


def test_create_empty_items_raises(db) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        PrescriptionTemplateController.create(doc, name="Preset", items=[])


def test_create_item_missing_drug_name_raises(db) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        PrescriptionTemplateController.create(
            doc, name="Preset", items=[{"dosage": "1 tablet", "frequency": "3x/day"}]
        )


def test_create_item_missing_dosage_raises(db) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        PrescriptionTemplateController.create(
            doc, name="Preset", items=[{"drug_name": "X", "frequency": "3x/day"}]
        )


def test_create_item_missing_frequency_raises(db) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        PrescriptionTemplateController.create(
            doc, name="Preset", items=[{"drug_name": "X", "dosage": "1 tablet"}]
        )


# --------------------------------------------------------------------------- #
# list_for
# --------------------------------------------------------------------------- #


def test_list_for_returns_only_own_templates_name_ordered(db) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    b = PrescriptionTemplateController.create(doc, name="Bronchite", items=[_item()])
    a = PrescriptionTemplateController.create(doc, name="Angine", items=[_item()])
    PrescriptionTemplateController.create(other, name="Theirs", items=[_item()])

    rows = PrescriptionTemplateController.list_for(doc)
    assert [r.id for r in rows] == [a.id, b.id]  # name-ordered, own only
    assert [r.name for r in rows] == ["Angine", "Bronchite"]


def test_list_for_empty(db) -> None:
    doc = _seed_doctor(db)
    assert PrescriptionTemplateController.list_for(doc) == []


# --------------------------------------------------------------------------- #
# delete
# --------------------------------------------------------------------------- #


def test_delete_removes_template(db) -> None:
    doc = _seed_doctor(db)
    row = PrescriptionTemplateController.create(doc, name="Preset", items=[_item()])
    PrescriptionTemplateController.delete(doc, row.id)
    assert PrescriptionTemplateController.list_for(doc) == []
    with db() as s:
        assert s.get(PrescriptionTemplate, row.id) is None


def test_delete_foreign_template_not_found(db) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    theirs = PrescriptionTemplateController.create(other, name="Theirs", items=[_item()])
    with pytest.raises(SehatyNotFoundError):
        PrescriptionTemplateController.delete(doc, theirs.id)
    # Still present for its owner.
    assert [r.id for r in PrescriptionTemplateController.list_for(other)] == [theirs.id]


def test_delete_missing_template_not_found(db) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyNotFoundError):
        PrescriptionTemplateController.delete(doc, 987654)
