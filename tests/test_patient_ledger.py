"""Patient treatment-ledger core tests on an in-memory SQLite engine.

Covers recording a charge (with and without a down payment), instalment
payments and the derived balance, overpayment / validation guards, doctor
scoping (foreign rows are NotFound), payment correction (delete), charge
deletion with payment cascade, and the practice-wide debtors roll-up.
Only the tables this feature touches (users, clinic_patients, patient_charges,
patient_payments) are created — none carry the PostGIS ``geopoint`` column.
"""

from datetime import UTC, datetime

import pytest
from sehaty.db import ClinicPatient, PatientCharge, PatientPayment, User, UserRole
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.patient_ledger import PatientLedgerController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError

_TABLES = [
    User.__table__,
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


def _seed_register(factory: sessionmaker[Session], doctor_id: int, full_name="Amina") -> int:
    with factory() as s:
        cp = ClinicPatient(doctor_id=doctor_id, full_name=full_name)
        s.add(cp)
        s.commit()
        return cp.id


def test_charge_with_down_payment_and_instalments(db):
    doctor = _seed_doctor(db)
    patient = _seed_register(db, doctor)

    charge = PatientLedgerController.add_charge(
        doctor, patient, created_by=doctor, label="Braces", total_amount=8000,
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
            doctor, patient, created_by=doctor, label="X", total_amount=100,
            initial_payment=200,
        )

    charge = PatientLedgerController.add_charge(
        doctor, patient, created_by=doctor, label="Cleaning", total_amount=400
    )
    with pytest.raises(SehatyValidationError):
        PatientLedgerController.add_payment(
            doctor, charge.id, created_by=doctor, amount=500  # exceeds balance
        )
    with pytest.raises(SehatyValidationError):
        PatientLedgerController.add_payment(
            doctor, charge.id, created_by=doctor, amount=100, method="BITCOIN"
        )
    with pytest.raises(SehatyValidationError):
        PatientLedgerController.add_payment(
            doctor, charge.id, created_by=doctor, amount=-5
        )


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
        doctor, charge.id, created_by=doctor, amount=1000,
        paid_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    wrong = PatientLedgerController.add_payment(
        doctor, charge.id, created_by=doctor, amount=999,
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
        doctor, small, created_by=doctor, label="Cleaning", total_amount=400,
        initial_payment=100,
    )
    # Two charges + two payments for one patient: the per-charge subquery must
    # not double-count either side.
    b1 = PatientLedgerController.add_charge(
        doctor, big, created_by=doctor, label="Braces", total_amount=8000,
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
