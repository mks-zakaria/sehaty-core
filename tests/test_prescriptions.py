"""Practice-profile (letterhead) + prescription core tests on in-memory SQLite.

Covers the single-default invariant on practice profiles (first-create forces
default, second with the flag flips it, set_default / delete-promotes-default,
foreign-row scoping) and the prescription flow: issuing freehand and
catalog-linked items, unique code/qr minting, item validation + quantity
default + app-patient link, the detail view with the letterhead snapshot, the
patient / app-patient list views (newest-first), cancellation, and cross-doctor
scoping. Only the tables these features touch are created.
"""

from datetime import UTC, datetime

import pytest
from sehaty.db import (
    ClinicPatient,
    Medication,
    PracticeProfile,
    Prescription,
    PrescriptionItem,
    PrescriptionStatus,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.practice import PracticeProfileController
from sehaty.core.controllers.prescriptions import PrescriptionController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import (
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)

_TABLES = [
    User.__table__,
    ClinicPatient.__table__,
    Medication.__table__,
    PracticeProfile.__table__,
    Prescription.__table__,
    PrescriptionItem.__table__,
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


def _seed_user(factory, *, email: str, role: UserRole) -> int:
    with factory() as s:
        user = User(email=email, role=role, is_active=True)
        s.add(user)
        s.commit()
        return user.id


def _seed_doctor(factory, email: str = "doc@clinic.ma") -> int:
    return _seed_user(factory, email=email, role=UserRole.DOCTOR)


def _seed_patient(factory, email: str = "pat@clinic.ma") -> int:
    return _seed_user(factory, email=email, role=UserRole.PATIENT)


def _seed_register(factory, doctor_id: int, *, user_id=None, full_name="Amina") -> int:
    with factory() as s:
        cp = ClinicPatient(doctor_id=doctor_id, user_id=user_id, full_name=full_name)
        s.add(cp)
        s.commit()
        return cp.id


def _seed_medication(factory, inn_name="Amoxicilline", brand_name="Clamoxyl") -> int:
    with factory() as s:
        med = Medication(inn_name=inn_name, brand_name=brand_name, form="tablet", strength="500mg")
        s.add(med)
        s.commit()
        return med.id


# --------------------------------------------------------------------------- #
# Practice profiles: single-default invariant
# --------------------------------------------------------------------------- #


def test_first_profile_forced_default(db) -> None:
    doc = _seed_doctor(db)
    p = PracticeProfileController.create(doc, name="Cabinet Zerktouni", is_default=False)
    # First profile is forced default despite is_default=False.
    assert p.is_default is True
    assert PracticeProfileController.get_default(doc).id == p.id


def test_second_profile_with_flag_flips_default(db) -> None:
    doc = _seed_doctor(db)
    first = PracticeProfileController.create(doc, name="Cabinet A")
    second = PracticeProfileController.create(doc, name="Cabinet B", is_default=True)

    rows = {r.id: r for r in PracticeProfileController.list_for(doc)}
    assert rows[second.id].is_default is True
    assert rows[first.id].is_default is False
    # Default-first ordering.
    assert PracticeProfileController.list_for(doc)[0].id == second.id
    assert PracticeProfileController.get_default(doc).id == second.id


def test_second_profile_without_flag_keeps_first_default(db) -> None:
    doc = _seed_doctor(db)
    first = PracticeProfileController.create(doc, name="Cabinet A")
    second = PracticeProfileController.create(doc, name="Cabinet B")
    assert PracticeProfileController.get_default(doc).id == first.id
    rows = {r.id: r for r in PracticeProfileController.list_for(doc)}
    assert rows[second.id].is_default is False


def test_set_default_switches(db) -> None:
    doc = _seed_doctor(db)
    a = PracticeProfileController.create(doc, name="A")
    b = PracticeProfileController.create(doc, name="B")
    PracticeProfileController.set_default(doc, b.id)
    assert PracticeProfileController.get_default(doc).id == b.id
    # Exactly one default remains.
    defaults = [r for r in PracticeProfileController.list_for(doc) if r.is_default]
    assert [r.id for r in defaults] == [b.id]
    # Switch back to a.
    PracticeProfileController.set_default(doc, a.id)
    assert PracticeProfileController.get_default(doc).id == a.id


def test_update_sets_default_and_fields(db) -> None:
    doc = _seed_doctor(db)
    a = PracticeProfileController.create(doc, name="A")
    b = PracticeProfileController.create(doc, name="B")
    updated = PracticeProfileController.update(doc, b.id, is_default=True, city="Casablanca")
    assert updated.is_default is True
    assert updated.city == "Casablanca"
    assert PracticeProfileController.get_default(doc).id == b.id
    # a is no longer default
    rows = {r.id: r for r in PracticeProfileController.list_for(doc)}
    assert rows[a.id].is_default is False


def test_delete_promotes_another_default(db) -> None:
    doc = _seed_doctor(db)
    a = PracticeProfileController.create(doc, name="A")  # default
    b = PracticeProfileController.create(doc, name="B")
    PracticeProfileController.delete(doc, a.id)
    # b is promoted to default since the default was removed.
    remaining = PracticeProfileController.list_for(doc)
    assert [r.id for r in remaining] == [b.id]
    assert PracticeProfileController.get_default(doc).id == b.id


def test_delete_non_default_keeps_default(db) -> None:
    doc = _seed_doctor(db)
    a = PracticeProfileController.create(doc, name="A")  # default
    b = PracticeProfileController.create(doc, name="B")
    PracticeProfileController.delete(doc, b.id)
    assert PracticeProfileController.get_default(doc).id == a.id


def test_delete_last_profile_leaves_no_default(db) -> None:
    doc = _seed_doctor(db)
    a = PracticeProfileController.create(doc, name="A")
    PracticeProfileController.delete(doc, a.id)
    assert PracticeProfileController.get_default(doc) is None
    assert PracticeProfileController.list_for(doc) == []


def test_create_empty_name_raises(db) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyValidationError):
        PracticeProfileController.create(doc, name="   ")


def test_profile_foreign_row_not_found(db) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    theirs = PracticeProfileController.create(other, name="Theirs")
    with pytest.raises(SehatyNotFoundError):
        PracticeProfileController.update(doc, theirs.id, city="X")
    with pytest.raises(SehatyNotFoundError):
        PracticeProfileController.set_default(doc, theirs.id)
    with pytest.raises(SehatyNotFoundError):
        PracticeProfileController.delete(doc, theirs.id)


def test_get_default_none_when_empty(db) -> None:
    doc = _seed_doctor(db)
    assert PracticeProfileController.get_default(doc) is None


# --------------------------------------------------------------------------- #
# Prescriptions
# --------------------------------------------------------------------------- #


def _freehand_item(**over) -> dict:
    base = {"drug_name": "Doliprane", "dosage": "1 tablet", "frequency": "3x/day"}
    base.update(over)
    return base


def test_create_freehand_persists_and_generates_code_qr(db) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc)
    PracticeProfileController.create(doc, name="Cabinet A")

    detail = PrescriptionController.create(
        doc,
        cp,
        items=[_freehand_item(duration_days=5)],
        notes="rest well",
    )
    assert detail.status == str(PrescriptionStatus.ISSUED)
    assert detail.notes == "rest well"
    assert len(detail.code) == 10
    assert len(detail.items) == 1
    item = detail.items[0]
    assert item.drug_name == "Doliprane"
    assert item.dosage == "1 tablet"
    assert item.quantity == 0  # defaulted (NOT NULL column)
    assert item.duration_days == 5
    # Uses the default letterhead.
    assert detail.practice_profile_id == PracticeProfileController.get_default(doc).id

    with db() as s:
        row = s.execute(select(Prescription).where(Prescription.id == detail.id)).scalar_one()
        assert len(row.qr_token) == 32
        assert row.code == detail.code
        assert row.expires_at > row.issued_at


def test_create_generates_unique_codes(db) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc)
    d1 = PrescriptionController.create(doc, cp, items=[_freehand_item()])
    d2 = PrescriptionController.create(doc, cp, items=[_freehand_item()])
    assert d1.code != d2.code
    with db() as s:
        tokens = s.execute(select(Prescription.qr_token)).scalars().all()
    assert len(tokens) == len(set(tokens)) == 2


def test_create_catalog_item_resolves_name(db) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc)
    med = _seed_medication(db, inn_name="Ibuprofene")
    detail = PrescriptionController.create(
        doc,
        cp,
        items=[{"medication_id": med, "dosage": "200mg", "frequency": "2x/day", "quantity": 20}],
    )
    assert detail.items[0].drug_name == "Ibuprofene"  # resolved from catalog
    assert detail.items[0].quantity == 20


def test_create_missing_drug_and_medication_raises(db) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc)
    with pytest.raises(SehatyValidationError):
        PrescriptionController.create(
            doc, cp, items=[{"dosage": "1 tablet", "frequency": "3x/day"}]
        )


def test_create_missing_dosage_raises(db) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc)
    with pytest.raises(SehatyValidationError):
        PrescriptionController.create(doc, cp, items=[{"drug_name": "X", "frequency": "3x/day"}])


def test_create_links_app_patient_user(db) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    cp = _seed_register(db, doc, user_id=pat)
    detail = PrescriptionController.create(doc, cp, items=[_freehand_item()])
    with db() as s:
        row = s.get(Prescription, detail.id)
        assert row.patient_id == pat  # app account link kept


def test_create_walkin_patient_no_user_link(db) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc, user_id=None)
    detail = PrescriptionController.create(doc, cp, items=[_freehand_item()])
    with db() as s:
        assert s.get(Prescription, detail.id).patient_id is None


def test_create_foreign_patient_not_found(db) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    cp = _seed_register(db, other)
    with pytest.raises(SehatyNotFoundError):
        PrescriptionController.create(doc, cp, items=[_freehand_item()])


def test_create_foreign_profile_not_found(db) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    cp = _seed_register(db, doc)
    theirs = PracticeProfileController.create(other, name="Theirs")
    with pytest.raises(SehatyNotFoundError):
        PrescriptionController.create(
            doc, cp, items=[_freehand_item()], practice_profile_id=theirs.id
        )


def test_get_returns_items_and_letterhead(db) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc)
    profile = PracticeProfileController.create(
        doc, name="Cabinet A", clinic_name="Clinique du Centre", city="Rabat"
    )
    detail = PrescriptionController.create(doc, cp, items=[_freehand_item()])

    got = PrescriptionController.get(doc, detail.id)
    assert got.id == detail.id
    assert len(got.items) == 1
    assert got.letterhead is not None
    assert got.letterhead.id == profile.id
    assert got.letterhead.clinic_name == "Clinique du Centre"
    assert got.letterhead.city == "Rabat"


def test_get_foreign_not_found(db) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    cp = _seed_register(db, other)
    detail = PrescriptionController.create(other, cp, items=[_freehand_item()])
    with pytest.raises(SehatyNotFoundError):
        PrescriptionController.get(doc, detail.id)


def test_list_for_patient_newest_first(db) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc)
    first = PrescriptionController.create(doc, cp, items=[_freehand_item()])
    second = PrescriptionController.create(
        doc, cp, items=[_freehand_item(), _freehand_item(drug_name="B")]
    )
    # Force distinct issued_at ordering deterministically.
    with db() as s:
        s.get(Prescription, first.id).issued_at = datetime(2026, 1, 1, tzinfo=UTC)
        s.get(Prescription, second.id).issued_at = datetime(2026, 6, 1, tzinfo=UTC)
        s.commit()

    rows = PrescriptionController.list_for_patient(doc, cp)
    assert [r.id for r in rows] == [second.id, first.id]  # newest-first
    assert rows[0].item_count == 2
    assert rows[1].item_count == 1


def test_list_for_patient_foreign_not_found(db) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    cp = _seed_register(db, other)
    with pytest.raises(SehatyNotFoundError):
        PrescriptionController.list_for_patient(doc, cp)


def test_list_for_app_patient_returns_own(db) -> None:
    pat = _seed_patient(db)
    doc1 = _seed_doctor(db, email="d1@clinic.ma")
    doc2 = _seed_doctor(db, email="d2@clinic.ma")
    other_pat = _seed_patient(db, email="p2@clinic.ma")

    cp1 = _seed_register(db, doc1, user_id=pat)
    cp2 = _seed_register(db, doc2, user_id=pat)
    cp_other = _seed_register(db, doc1, user_id=other_pat)

    p1 = PrescriptionController.create(doc1, cp1, items=[_freehand_item()])
    p2 = PrescriptionController.create(doc2, cp2, items=[_freehand_item()])
    PrescriptionController.create(doc1, cp_other, items=[_freehand_item()])

    rows = PrescriptionController.list_for_app_patient(pat)
    assert {r.id for r in rows} == {p1.id, p2.id}  # only this patient's own


def test_cancel_sets_cancelled(db) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc)
    detail = PrescriptionController.create(doc, cp, items=[_freehand_item()])
    cancelled = PrescriptionController.cancel(doc, detail.id)
    assert cancelled.status == str(PrescriptionStatus.CANCELLED)
    with db() as s:
        assert s.get(Prescription, detail.id).status == PrescriptionStatus.CANCELLED


def test_cancel_foreign_not_found(db) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    cp = _seed_register(db, other)
    detail = PrescriptionController.create(other, cp, items=[_freehand_item()])
    with pytest.raises(SehatyNotFoundError):
        PrescriptionController.cancel(doc, detail.id)


# --------------------------------------------------------------------------- #
# Patient-scoped detail: get_for_app_patient
# --------------------------------------------------------------------------- #


def test_get_for_app_patient_returns_items_for_owner(db) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    cp = _seed_register(db, doc, user_id=pat)
    med = _seed_medication(db, inn_name="Paracetamol")
    detail = PrescriptionController.create(
        doc,
        cp,
        items=[
            _freehand_item(drug_name="Doliprane", dosage="1 tablet", frequency="3x/day"),
            {"medication_id": med, "dosage": "500mg", "frequency": "2x/day", "quantity": 12},
        ],
    )

    got = PrescriptionController.get_for_app_patient(pat, detail.id)
    assert got.id == detail.id
    assert got.status == str(PrescriptionStatus.ISSUED)
    names = {i.drug_name for i in got.items}
    assert names == {"Doliprane", "Paracetamol"}  # freehand + resolved catalog
    assert len(got.items) == 2


def test_get_for_app_patient_forbidden_for_other_user(db) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    intruder = _seed_patient(db, email="intruder@clinic.ma")
    cp = _seed_register(db, doc, user_id=pat)
    detail = PrescriptionController.create(doc, cp, items=[_freehand_item()])

    with pytest.raises(SehatyForbiddenError):
        PrescriptionController.get_for_app_patient(intruder, detail.id)


def test_get_for_app_patient_walkin_forbidden(db) -> None:
    # Prescription for a walk-in (no linked user) -> no app patient may view it.
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    cp = _seed_register(db, doc, user_id=None)
    detail = PrescriptionController.create(doc, cp, items=[_freehand_item()])

    with pytest.raises(SehatyForbiddenError):
        PrescriptionController.get_for_app_patient(pat, detail.id)


def test_get_for_app_patient_unknown_id_not_found(db) -> None:
    pat = _seed_patient(db)
    with pytest.raises(SehatyNotFoundError):
        PrescriptionController.get_for_app_patient(pat, 987654)
