"""ExportController tests on an in-memory SQLite engine.

Seeds a doctor with a register patient, appointments (one with a recorded
consultation), a diagnosis and a prescription, then checks the exported sheets
are doctor-scoped and shaped as expected.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    ClinicPatient,
    Diagnosis,
    Invoice,
    InvoiceStatus,
    Medication,
    Prescription,
    PrescriptionItem,
    PrescriptionStatus,
    Review,
    ReviewDirection,
    ReviewStatus,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.export import ExportController
from sehaty.core.db import session as session_mod

_NOW = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)

_TABLES = [
    User.__table__,
    ClinicPatient.__table__,
    Appointment.__table__,
    Diagnosis.__table__,
    Prescription.__table__,
    PrescriptionItem.__table__,
    Medication.__table__,
    Review.__table__,
    Invoice.__table__,
]


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _seed(factory) -> int:
    with factory() as s:
        doctor = User(email="exp@clinic.ma", role=UserRole.DOCTOR, is_active=True)
        other = User(email="other@clinic.ma", role=UserRole.DOCTOR, is_active=True)
        s.add_all([doctor, other])
        s.flush()
        cp = ClinicPatient(doctor_id=doctor.id, full_name="Yassine T.", phone="+2126")
        other_cp = ClinicPatient(doctor_id=other.id, full_name="Not Mine")
        s.add_all([cp, other_cp])
        s.flush()
        rx = Prescription(
            doctor_id=doctor.id, clinic_patient_id=cp.id, code="RX-1", qr_token="tok-1",
            status=PrescriptionStatus.ISSUED, issued_at=_NOW,
            expires_at=_NOW + timedelta(days=30),
        )
        appt_done = Appointment(
            patient_id=doctor.id, doctor_id=doctor.id, clinic_patient_id=cp.id,
            start_at=_NOW - timedelta(days=1), end_at=_NOW - timedelta(days=1),
            status=AppointmentStatus.COMPLETED,
            consultation_started_at=_NOW - timedelta(days=1),
            consultation_ended_at=_NOW - timedelta(days=1),
            chief_complaint="cough", vitals={"temp_c": 38.2},
        )
        s.add_all([
            Appointment(
                patient_id=doctor.id, doctor_id=doctor.id, clinic_patient_id=cp.id,
                start_at=_NOW, end_at=_NOW + timedelta(minutes=30),
                status=AppointmentStatus.CONFIRMED,
            ),
            appt_done,
            # A completed appointment for another doctor — must NOT appear.
            Appointment(
                patient_id=other.id, doctor_id=other.id, clinic_patient_id=other_cp.id,
                start_at=_NOW, end_at=_NOW, status=AppointmentStatus.COMPLETED,
                consultation_started_at=_NOW,
            ),
        ])
        s.flush()
        s.add_all([
            Diagnosis(
                doctor_id=doctor.id, clinic_patient_id=cp.id, label="Flu",
                diagnosed_at=_NOW,
            ),
            rx,
            Review(
                author_id=other.id, target_id=doctor.id, appointment_id=appt_done.id,
                direction=ReviewDirection.PATIENT_ON_DOCTOR, stars=5,
                comment="Great doctor", status=ReviewStatus.PUBLISHED,
            ),
            # A review the doctor authored about a patient — NOT their inbound reviews.
            Review(
                author_id=doctor.id, target_id=other.id, appointment_id=appt_done.id,
                direction=ReviewDirection.DOCTOR_ON_PATIENT, stars=3, status=ReviewStatus.PUBLISHED,
            ),
            Invoice(
                doctor_id=doctor.id, amount=200.0, currency="MAD",
                status=InvoiceStatus.PAID, issued_at=_NOW, due_at=_NOW + timedelta(days=7),
                paid_at=_NOW,
            ),
        ])
        s.flush()
        s.add(
            PrescriptionItem(
                prescription_id=rx.id, drug_name="Amoxicillin", dosage="1 tablet",
                frequency="twice a day", quantity=14, duration_days=7,
            )
        )
        s.commit()
        return doctor.id


def test_doctor_export_sheets(db):
    doctor_id = _seed(db)
    sheets = {sh.title: sh for sh in ExportController.doctor_export(doctor_id)}

    assert list(sheets) == [
        "Patients", "Appointments", "Consultations", "Diagnoses",
        "Prescriptions", "Prescription Items", "Reviews", "Billing",
    ]

    # Patients: one row, doctor-scoped (the other doctor's patient is excluded).
    assert len(sheets["Patients"].rows) == 1
    assert "Yassine T." in sheets["Patients"].rows[0]

    # Appointments: both of this doctor's, none from the other doctor.
    assert len(sheets["Appointments"].rows) == 2

    # Consultations: only the appointment that was actually started.
    consults = sheets["Consultations"]
    assert len(consults.rows) == 1
    row = consults.rows[0]
    assert row[consults.columns.index("patient")] == "Yassine T."
    assert row[consults.columns.index("chief_complaint")] == "cough"
    # JSON vitals are flattened to a string cell.
    assert row[consults.columns.index("vitals")] == '{"temp_c":38.2}'

    assert len(sheets["Diagnoses"].rows) == 1
    assert "Flu" in sheets["Diagnoses"].rows[0]
    assert len(sheets["Prescriptions"].rows) == 1
    assert "RX-1" in sheets["Prescriptions"].rows[0]

    # Prescription Items: one drug line, linked to its prescription + patient.
    items = sheets["Prescription Items"]
    assert len(items.rows) == 1
    item = items.rows[0]
    assert item[items.columns.index("prescription_code")] == "RX-1"
    assert item[items.columns.index("patient")] == "Yassine T."
    assert item[items.columns.index("drug")] == "Amoxicillin"
    assert item[items.columns.index("quantity")] == 14

    # Reviews: only inbound (patient-on-doctor) reviews, not ones the doctor wrote.
    reviews = sheets["Reviews"]
    assert len(reviews.rows) == 1
    assert reviews.rows[0][reviews.columns.index("stars")] == 5
    assert reviews.rows[0][reviews.columns.index("comment")] == "Great doctor"

    # Billing: the doctor's invoices.
    billing = sheets["Billing"]
    assert len(billing.rows) == 1
    assert billing.rows[0][billing.columns.index("amount")] == 200.0


def test_export_empty_for_new_doctor(db):
    with db() as s:
        doc = User(email="new@clinic.ma", role=UserRole.DOCTOR, is_active=True)
        s.add(doc)
        s.commit()
        doctor_id = doc.id
    sheets = ExportController.doctor_export(doctor_id)
    assert len(sheets) == 8
    assert all(sh.rows == [] for sh in sheets)
