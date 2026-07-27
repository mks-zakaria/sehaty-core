"""Patient treatment-ledger core tests on an in-memory SQLite engine.

Covers recording a charge (with and without a down payment), instalment
payments and the derived balance, overpayment / validation guards, doctor
scoping (foreign rows are NotFound), payment correction (delete), charge
deletion with payment cascade, the practice-wide debtors roll-up, and the
patient-facing ``my_debts`` view across doctors. ``doctor_profiles`` is created
too (for the doctor-name join), so the PostGIS ``geography`` type is shimmed to
TEXT for SQLite.
"""

from datetime import UTC, datetime

import pytest
from geoalchemy2 import Geography
from geoalchemy2 import functions as geo_functions
from sehaty.db import (
    ClinicPatient,
    DoctorProfile,
    PatientCharge,
    PatientPayment,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.patient_ledger import PatientLedgerController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError


@compiles(Geography, "sqlite")
def _compile_geography_sqlite(type_, compiler, **kw) -> str:  # noqa: ANN001
    """Render the PostGIS ``geography`` column as TEXT so SQLite can build it."""
    return "TEXT"


@compiles(geo_functions.ST_GeogFromText, "sqlite")
def _geog_bind_passthrough_on_sqlite(element, compiler, **kw) -> str:  # noqa: ANN001
    """Skip the PostGIS constructor SQLite lacks; bind the raw value instead."""
    return compiler.process(list(element.clauses)[0], **kw)


_TABLES = [
    User.__table__,
    DoctorProfile.__table__,
    ClinicPatient.__table__,
    PatientCharge.__table__,
    PatientPayment.__table__,
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


def _seed_doctor(factory: sessionmaker[Session], email: str = "doc@clinic.ma") -> int:
    with factory() as s:
        user = User(email=email, role=UserRole.DOCTOR, is_active=True)
        s.add(user)
        s.commit()
        return user.id


def _seed_register(
    factory: sessionmaker[Session], doctor_id: int, full_name="Amina", user_id=None
) -> int:
    with factory() as s:
        cp = ClinicPatient(doctor_id=doctor_id, full_name=full_name, user_id=user_id)
        s.add(cp)
        s.commit()
        return cp.id


def _seed_doctor_profile(factory, doctor_id: int, name: str, slug: str) -> None:
    with factory() as s:
        s.add(DoctorProfile(user_id=doctor_id, full_name=name, slug=slug, license_no=slug))
        s.commit()


def _seed_app_patient(factory, email="pat@app.ma") -> int:
    with factory() as s:
        u = User(email=email, role=UserRole.PATIENT, is_active=True)
        s.add(u)
        s.commit()
        return u.id


def test_charge_with_down_payment_and_instalments(db):
    doctor = _seed_doctor(db)
    patient = _seed_register(db, doctor)

    charge = PatientLedgerController.add_charge(
        doctor,
        patient,
        created_by=doctor,
        label="Braces",
        total_amount=8000,
        initial_payment=3000,
    )
    assert charge.paid_amount == 3000
    assert charge.balance == 5000

    charge = PatientLedgerController.add_payment(
        doctor, charge.id, created_by=doctor, amount=2500, method="CARD"
    )
    assert charge.paid_amount == 5500
    assert charge.balance == 2500
    assert [p.method for p in charge.payments] == ["CASH", "CARD"]

    ledger = PatientLedgerController.list_charges(doctor, patient)
    assert ledger.total_charged == 8000
    assert ledger.total_paid == 5500
    assert ledger.total_outstanding == 2500


def test_validation_guards(db):
    doctor = _seed_doctor(db)
    patient = _seed_register(db, doctor)

    with pytest.raises(SehatyValidationError):
        PatientLedgerController.add_charge(
            doctor, patient, created_by=doctor, label="  ", total_amount=100
        )
    with pytest.raises(SehatyValidationError):
        PatientLedgerController.add_charge(
            doctor, patient, created_by=doctor, label="X", total_amount=0
        )
    with pytest.raises(SehatyValidationError):
        PatientLedgerController.add_charge(
            doctor,
            patient,
            created_by=doctor,
            label="X",
            total_amount=100,
            initial_payment=200,
        )

    charge = PatientLedgerController.add_charge(
        doctor, patient, created_by=doctor, label="Cleaning", total_amount=400
    )
    with pytest.raises(SehatyValidationError):
        PatientLedgerController.add_payment(
            doctor,
            charge.id,
            created_by=doctor,
            amount=500,  # exceeds balance
        )
    with pytest.raises(SehatyValidationError):
        PatientLedgerController.add_payment(
            doctor, charge.id, created_by=doctor, amount=100, method="BITCOIN"
        )
    with pytest.raises(SehatyValidationError):
        PatientLedgerController.add_payment(doctor, charge.id, created_by=doctor, amount=-5)


def test_doctor_scoping(db):
    doctor = _seed_doctor(db)
    other = _seed_doctor(db, email="other@clinic.ma")
    patient = _seed_register(db, doctor)
    charge = PatientLedgerController.add_charge(
        doctor, patient, created_by=doctor, label="Braces", total_amount=8000
    )

    with pytest.raises(SehatyNotFoundError):
        PatientLedgerController.list_charges(other, patient)
    with pytest.raises(SehatyNotFoundError):
        PatientLedgerController.add_charge(
            other, patient, created_by=other, label="X", total_amount=10
        )
    with pytest.raises(SehatyNotFoundError):
        PatientLedgerController.add_payment(other, charge.id, created_by=other, amount=10)
    with pytest.raises(SehatyNotFoundError):
        PatientLedgerController.delete_charge(other, charge.id)


def test_payment_correction_and_charge_delete(db):
    doctor = _seed_doctor(db)
    patient = _seed_register(db, doctor)
    charge = PatientLedgerController.add_charge(
        doctor, patient, created_by=doctor, label="Braces", total_amount=8000
    )
    charge = PatientLedgerController.add_payment(
        doctor,
        charge.id,
        created_by=doctor,
        amount=1000,
        paid_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    wrong = PatientLedgerController.add_payment(
        doctor,
        charge.id,
        created_by=doctor,
        amount=999,
        paid_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    fixed = PatientLedgerController.delete_payment(
        doctor, charge.id, payment_id=wrong.payments[-1].id
    )
    assert fixed.paid_amount == 1000
    assert fixed.balance == 7000

    with pytest.raises(SehatyNotFoundError):
        PatientLedgerController.delete_payment(doctor, charge.id, payment_id=424242)

    PatientLedgerController.delete_charge(doctor, charge.id)
    ledger = PatientLedgerController.list_charges(doctor, patient)
    assert ledger.charges == []
    assert ledger.total_outstanding == 0


def test_debtors_rollup(db):
    doctor = _seed_doctor(db)
    paid_up = _seed_register(db, doctor, full_name="Paid Up")
    small = _seed_register(db, doctor, full_name="Small Debt")
    big = _seed_register(db, doctor, full_name="Big Debt")

    c = PatientLedgerController.add_charge(
        doctor, paid_up, created_by=doctor, label="Filling", total_amount=500
    )
    PatientLedgerController.add_payment(doctor, c.id, created_by=doctor, amount=500)

    PatientLedgerController.add_charge(
        doctor,
        small,
        created_by=doctor,
        label="Cleaning",
        total_amount=400,
        initial_payment=100,
    )
    # Two charges + two payments for one patient: the per-charge subquery must
    # not double-count either side.
    b1 = PatientLedgerController.add_charge(
        doctor,
        big,
        created_by=doctor,
        label="Braces",
        total_amount=8000,
        initial_payment=2000,
    )
    PatientLedgerController.add_payment(doctor, b1.id, created_by=doctor, amount=1000)
    PatientLedgerController.add_charge(
        doctor, big, created_by=doctor, label="Extraction", total_amount=700
    )

    debtors = PatientLedgerController.list_debtors(doctor)
    assert [(d.full_name, d.balance) for d in debtors] == [
        ("Big Debt", 5700.0),
        ("Small Debt", 300.0),
    ]
    assert debtors[0].total_charged == 8700.0
    assert debtors[0].total_paid == 3000.0


def test_my_debts_across_doctors(db):
    """A patient sees charges from every doctor whose register links their account."""
    app_patient = _seed_app_patient(db)
    d1 = _seed_doctor(db, email="d1@clinic.ma")
    d2 = _seed_doctor(db, email="d2@clinic.ma")
    _seed_doctor_profile(db, d1, "Dr. Amina Bennani", "dr-bennani")
    _seed_doctor_profile(db, d2, "Dr. Omar Saidi", "dr-saidi")
    # The same app patient is a register row for both doctors.
    reg1 = _seed_register(db, d1, full_name="Mehdi", user_id=app_patient)
    reg2 = _seed_register(db, d2, full_name="Mehdi", user_id=app_patient)
    # A different patient's charge that must NOT leak into the summary.
    other = _seed_register(db, d1, full_name="Someone else")

    c1 = PatientLedgerController.add_charge(
        d1, reg1, created_by=d1, label="Braces", total_amount=8000, initial_payment=3000
    )
    PatientLedgerController.add_charge(
        d2, reg2, created_by=d2, label="Cleaning", total_amount=400, initial_payment=400
    )  # settled
    PatientLedgerController.add_charge(
        d1, other, created_by=d1, label="Root canal", total_amount=1500
    )

    summary = PatientLedgerController.my_debts(app_patient)
    # Two charges belong to this patient (the "other" one is excluded).
    assert len(summary.charges) == 2
    labels = {c.label for c in summary.charges}
    assert labels == {"Braces", "Cleaning"}
    # Only the unpaid balance counts toward the total (braces 5000, cleaning 0).
    assert summary.total_outstanding == 5000.0
    braces = next(c for c in summary.charges if c.label == "Braces")
    assert braces.doctor_name == "Dr. Amina Bennani"
    assert braces.doctor_slug == "dr-bennani"
    assert braces.balance == 5000.0
    assert braces.id == c1.id

    # A patient with no register links owes nothing.
    empty = PatientLedgerController.my_debts(_seed_app_patient(db, email="nobody@app.ma"))
    assert empty.charges == []
    assert empty.total_outstanding == 0
