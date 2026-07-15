"""Diagnosis + treatment-feedback core tests on an in-memory SQLite engine.

Covers the doctor-scoped diagnosis flows (create validates label + ownership,
newest-first listing, the patient's own-diagnoses view, cross-doctor
NotFound) and the patient treatment-feedback flow (a patient rates their OWN
record, a different user is Forbidden, leaving again upserts rather than
duplicates, invalid outcomes raise Validation, and the owning doctor can list
the feedback). Only the tables these features touch are created.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    ClinicPatient,
    Diagnosis,
    Prescription,
    PrescriptionStatus,
    TreatmentFeedback,
    TreatmentOutcome,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.diagnoses import DiagnosisController
from sehaty.core.controllers.feedback import TreatmentFeedbackController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import (
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)

_TABLES = [
    User.__table__,
    ClinicPatient.__table__,
    Appointment.__table__,
    Prescription.__table__,
    Diagnosis.__table__,
    TreatmentFeedback.__table__,
]

_NOW = datetime(2026, 7, 13, 9, 0, tzinfo=UTC)


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


def _seed_register(factory, doctor_id: int, *, user_id=None, full_name=None) -> int:
    with factory() as s:
        cp = ClinicPatient(doctor_id=doctor_id, user_id=user_id, full_name=full_name)
        s.add(cp)
        s.commit()
        return cp.id


def _seed_prescription(factory, doctor_id: int, clinic_patient_id: int | None) -> int:
    with factory() as s:
        p = Prescription(
            doctor_id=doctor_id,
            clinic_patient_id=clinic_patient_id,
            code=f"RX{clinic_patient_id}-{doctor_id}",
            qr_token=f"tok{clinic_patient_id}-{doctor_id}",
            status=PrescriptionStatus.ISSUED,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(days=30),
        )
        s.add(p)
        s.commit()
        return p.id


def _seed_appointment(
    factory, doctor_id: int, patient_id: int, clinic_patient_id: int | None
) -> int:
    with factory() as s:
        a = Appointment(
            patient_id=patient_id,
            doctor_id=doctor_id,
            clinic_patient_id=clinic_patient_id,
            start_at=_NOW,
            end_at=_NOW + timedelta(minutes=30),
            status=AppointmentStatus.COMPLETED,
        )
        s.add(a)
        s.commit()
        return a.id


# --------------------------------------------------------------------------- #
# Diagnosis
# --------------------------------------------------------------------------- #


def test_create_diagnosis(db) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc, full_name="Amina")
    row = DiagnosisController.create(doc, cp, label="Hypertension", icd10="I10", notes="mild")
    assert row.label == "Hypertension"
    assert row.icd10 == "I10"
    assert row.notes == "mild"
    assert row.diagnosed_at is not None  # defaulted to now
    with db() as s:
        stored = s.get(Diagnosis, row.id)
        assert stored.doctor_id == doc
        assert stored.clinic_patient_id == cp


def test_create_diagnosis_empty_label_raises(db) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc, full_name="Amina")
    with pytest.raises(SehatyValidationError):
        DiagnosisController.create(doc, cp, label="   ")


def test_create_diagnosis_foreign_patient_not_found(db) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    cp = _seed_register(db, other, full_name="Theirs")
    with pytest.raises(SehatyNotFoundError):
        DiagnosisController.create(doc, cp, label="Flu")


def test_list_for_patient_newest_first(db) -> None:
    doc = _seed_doctor(db)
    cp = _seed_register(db, doc, full_name="Amina")
    DiagnosisController.create(doc, cp, label="Old", diagnosed_at=_NOW - timedelta(days=10))
    DiagnosisController.create(doc, cp, label="New", diagnosed_at=_NOW)

    rows = DiagnosisController.list_for_patient(doc, cp)
    assert [r.label for r in rows] == ["New", "Old"]


def test_list_for_patient_foreign_not_found(db) -> None:
    doc = _seed_doctor(db, email="d1@clinic.ma")
    other = _seed_doctor(db, email="d2@clinic.ma")
    cp = _seed_register(db, other, full_name="Theirs")
    with pytest.raises(SehatyNotFoundError):
        DiagnosisController.list_for_patient(doc, cp)


def test_list_for_app_patient_returns_own(db) -> None:
    doc1 = _seed_doctor(db, email="d1@clinic.ma")
    doc2 = _seed_doctor(db, email="d2@clinic.ma")
    pat = _seed_patient(db)
    other_pat = _seed_patient(db, email="other@clinic.ma")

    cp1 = _seed_register(db, doc1, user_id=pat)
    cp2 = _seed_register(db, doc2, user_id=pat)
    cp_other = _seed_register(db, doc1, user_id=other_pat)

    DiagnosisController.create(doc1, cp1, label="A", diagnosed_at=_NOW - timedelta(days=1))
    DiagnosisController.create(doc2, cp2, label="B", diagnosed_at=_NOW)
    DiagnosisController.create(doc1, cp_other, label="NotMine")

    rows = DiagnosisController.list_for_app_patient(pat)
    # Own diagnoses across both doctors, newest first; not the other patient's.
    assert [r.label for r in rows] == ["B", "A"]


def test_update_and_delete_diagnosis(db) -> None:
    doc = _seed_doctor(db)
    other = _seed_doctor(db, email="d2@clinic.ma")
    cp = _seed_register(db, doc, full_name="Amina")
    row = DiagnosisController.create(doc, cp, label="Flu")

    updated = DiagnosisController.update(doc, row.id, notes="resolved", bogus="x")
    assert updated.notes == "resolved"

    # Foreign doctor cannot update or delete.
    with pytest.raises(SehatyNotFoundError):
        DiagnosisController.update(other, row.id, notes="hack")
    with pytest.raises(SehatyNotFoundError):
        DiagnosisController.delete(other, row.id)

    DiagnosisController.delete(doc, row.id)
    with db() as s:
        assert s.get(Diagnosis, row.id) is None


# --------------------------------------------------------------------------- #
# Treatment feedback
# --------------------------------------------------------------------------- #


def test_leave_feedback_on_own_diagnosis(db) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    cp = _seed_register(db, doc, user_id=pat)
    dx = DiagnosisController.create(doc, cp, label="Flu")

    fb = TreatmentFeedbackController.leave(pat, "diagnosis", dx.id, "BETTER", comment="cured")
    assert fb.outcome == "BETTER"
    assert fb.target_type == "diagnosis"
    assert fb.target_id == dx.id
    assert fb.comment == "cured"
    with db() as s:
        stored = s.get(TreatmentFeedback, fb.id)
        assert stored.clinic_patient_id == cp
        assert stored.outcome == TreatmentOutcome.BETTER


def test_leave_feedback_other_user_forbidden(db) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    intruder = _seed_patient(db, email="intruder@clinic.ma")
    cp = _seed_register(db, doc, user_id=pat)
    dx = DiagnosisController.create(doc, cp, label="Flu")

    with pytest.raises(SehatyForbiddenError):
        TreatmentFeedbackController.leave(intruder, "diagnosis", dx.id, "WORSE")


def test_leave_feedback_upserts(db) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    cp = _seed_register(db, doc, user_id=pat)
    dx = DiagnosisController.create(doc, cp, label="Flu")

    first = TreatmentFeedbackController.leave(pat, "diagnosis", dx.id, "SAME")
    second = TreatmentFeedbackController.leave(pat, "diagnosis", dx.id, "BETTER", comment="now ok")

    assert first.id == second.id  # same row, updated in place
    assert second.outcome == "BETTER"
    assert second.comment == "now ok"
    rows = TreatmentFeedbackController.list_for_patient(doc, cp)
    assert len(rows) == 1


def test_leave_feedback_invalid_outcome_raises(db) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    cp = _seed_register(db, doc, user_id=pat)
    dx = DiagnosisController.create(doc, cp, label="Flu")

    with pytest.raises(SehatyValidationError):
        TreatmentFeedbackController.leave(pat, "diagnosis", dx.id, "AMAZING")


def test_leave_feedback_unknown_target_not_found(db) -> None:
    pat = _seed_patient(db)
    with pytest.raises(SehatyNotFoundError):
        TreatmentFeedbackController.leave(pat, "diagnosis", 999, "BETTER")
    with pytest.raises(SehatyNotFoundError):
        TreatmentFeedbackController.leave(pat, "bogus", 1, "BETTER")


def test_leave_feedback_on_prescription_and_appointment(db) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    cp = _seed_register(db, doc, user_id=pat)
    rx = _seed_prescription(db, doc, cp)
    appt = _seed_appointment(db, doc, pat, cp)

    fb_rx = TreatmentFeedbackController.leave(pat, "prescription", rx, "BETTER")
    fb_appt = TreatmentFeedbackController.leave(pat, "appointment", appt, "SAME")
    assert fb_rx.outcome == "BETTER"
    assert fb_appt.outcome == "SAME"


def test_list_for_patient_doctor_view(db) -> None:
    doc = _seed_doctor(db)
    other = _seed_doctor(db, email="d2@clinic.ma")
    pat = _seed_patient(db)
    cp = _seed_register(db, doc, user_id=pat)
    dx = DiagnosisController.create(doc, cp, label="Flu")
    TreatmentFeedbackController.leave(pat, "diagnosis", dx.id, "BETTER")

    rows = TreatmentFeedbackController.list_for_patient(doc, cp)
    assert len(rows) == 1
    assert rows[0].outcome == "BETTER"

    # A foreign doctor cannot view this register patient's feedback.
    with pytest.raises(SehatyNotFoundError):
        TreatmentFeedbackController.list_for_patient(other, cp)


def test_for_targets_maps_latest(db) -> None:
    doc = _seed_doctor(db)
    pat = _seed_patient(db)
    cp = _seed_register(db, doc, user_id=pat)
    dx = DiagnosisController.create(doc, cp, label="Flu")
    rx = _seed_prescription(db, doc, cp)
    TreatmentFeedbackController.leave(pat, "diagnosis", dx.id, "BETTER")
    TreatmentFeedbackController.leave(pat, "prescription", rx, "SAME")

    mapping = TreatmentFeedbackController.for_targets(doc, cp)
    assert mapping[("diagnosis", dx.id)].outcome == "BETTER"
    assert mapping[("prescription", rx)].outcome == "SAME"
