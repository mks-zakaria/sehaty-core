"""Patient treatment-ledger business logic (patient_charges / patient_payments).

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A doctor
records a treatment charge (e.g. braces at 8000 MAD) against one of their
register patients, then records instalment payments against it over time. The
outstanding balance is always derived (``total_amount - sum(payments)``) —
never stored — so it cannot drift.

Everything is doctor-scoped: every method takes ``doctor_id`` and verifies the
charge/patient belongs to that doctor, so foreign rows raise
:class:`SehatyNotFoundError`. Reads return detached frozen projections, never
ORM objects. Failures raise the ``SehatyError`` taxonomy; methods never return
``None`` to signal an error.
"""

from datetime import UTC, datetime

from sehaty.db import ClinicPatient, PatientCharge, PatientPayment, PaymentMethod
from sqlalchemy import func, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError


class PaymentRow(DomainModel):
    """One instalment payment against a charge (detached projection)."""

    id: int
    amount: float
    method: str
    paid_at: datetime
    note: str | None


class ChargeRow(DomainModel):
    """One treatment charge with its payments and derived balance."""

    id: int
    clinic_patient_id: int
    label: str
    total_amount: float
    currency: str
    note: str | None
    paid_amount: float
    balance: float
    created_at: datetime
    payments: list[PaymentRow]


class PatientLedgerSummary(DomainModel):
    """A patient's ledger: all charges plus rolled-up totals."""

    charges: list[ChargeRow]
    total_charged: float
    total_paid: float
    total_outstanding: float


class DebtorRow(DomainModel):
    """One register patient with money still owed (practice-wide view)."""

    clinic_patient_id: int
    full_name: str | None
    phone: str | None
    total_charged: float
    total_paid: float
    balance: float


def _charge_row(charge: PatientCharge) -> ChargeRow:
    payments = sorted(charge.payments, key=lambda p: (p.paid_at, p.id))
    paid = float(sum(p.amount for p in payments))
    return ChargeRow(
        id=charge.id,
        clinic_patient_id=charge.clinic_patient_id,
        label=charge.label,
        total_amount=float(charge.total_amount),
        currency=charge.currency,
        note=charge.note,
        paid_amount=paid,
        balance=round(float(charge.total_amount) - paid, 2),
        created_at=charge.created_at,
        payments=[
            PaymentRow(
                id=p.id,
                amount=float(p.amount),
                method=str(p.method),
                paid_at=p.paid_at,
                note=p.note,
            )
            for p in payments
        ],
    )


def _own_patient(session, doctor_id: int, patient_id: int) -> ClinicPatient:
    patient = session.execute(
        select(ClinicPatient).where(
            ClinicPatient.id == patient_id, ClinicPatient.doctor_id == doctor_id
        )
    ).scalar_one_or_none()
    if patient is None:
        raise SehatyNotFoundError(f"no patient {patient_id} in doctor {doctor_id}'s register")
    return patient


def _own_charge(session, doctor_id: int, charge_id: int) -> PatientCharge:
    charge = session.execute(
        select(PatientCharge).where(
            PatientCharge.id == charge_id, PatientCharge.doctor_id == doctor_id
        )
    ).scalar_one_or_none()
    if charge is None:
        raise SehatyNotFoundError(f"no charge {charge_id} for doctor {doctor_id}")
    return charge


class PatientLedgerController:
    @staticmethod
    def list_charges(doctor_id: int, patient_id: int) -> PatientLedgerSummary:
        """The patient's full ledger, newest charge first, with rolled-up totals.

        The register row must belong to ``doctor_id`` (else
        :class:`SehatyNotFoundError`).
        """
        with get_session() as session:
            _own_patient(session, doctor_id, patient_id)
            charges = (
                session.execute(
                    select(PatientCharge)
                    .where(
                        PatientCharge.clinic_patient_id == patient_id,
                        PatientCharge.doctor_id == doctor_id,
                    )
                    .order_by(PatientCharge.created_at.desc(), PatientCharge.id.desc())
                )
                .scalars()
                .all()
            )
            rows = [_charge_row(c) for c in charges]
        total_charged = round(sum(r.total_amount for r in rows), 2)
        total_paid = round(sum(r.paid_amount for r in rows), 2)
        return PatientLedgerSummary(
            charges=rows,
            total_charged=total_charged,
            total_paid=total_paid,
            total_outstanding=round(total_charged - total_paid, 2),
        )

    @staticmethod
    def add_charge(
        doctor_id: int,
        patient_id: int,
        created_by: int,
        label: str,
        total_amount: float,
        currency: str = "MAD",
        note: str | None = None,
        initial_payment: float | None = None,
    ) -> ChargeRow:
        """Record a treatment charge, optionally with a same-visit down payment.

        ``label`` must be non-empty and ``total_amount`` strictly positive;
        ``initial_payment`` (when given) must be positive and no more than the
        total — all else :class:`SehatyValidationError`. The register row must
        belong to ``doctor_id``.
        """
        name = (label or "").strip()
        if not name:
            raise SehatyValidationError("label must not be empty")
        if total_amount <= 0:
            raise SehatyValidationError("total_amount must be positive")
        if initial_payment is not None:
            if initial_payment <= 0:
                raise SehatyValidationError("initial_payment must be positive")
            if initial_payment > total_amount:
                raise SehatyValidationError("initial_payment cannot exceed total_amount")
        with get_session() as session:
            _own_patient(session, doctor_id, patient_id)
            charge = PatientCharge(
                doctor_id=doctor_id,
                clinic_patient_id=patient_id,
                label=name,
                total_amount=float(total_amount),
                currency=currency,
                note=note,
                created_by=created_by,
            )
            if initial_payment is not None:
                charge.payments.append(
                    PatientPayment(
                        amount=float(initial_payment),
                        method=PaymentMethod.CASH,
                        paid_at=datetime.now(UTC),
                        created_by=created_by,
                    )
                )
            session.add(charge)
            session.flush()
            row = _charge_row(charge)
        return row

    @staticmethod
    def add_payment(
        doctor_id: int,
        charge_id: int,
        created_by: int,
        amount: float,
        method: str = "CASH",
        note: str | None = None,
        paid_at: datetime | None = None,
    ) -> ChargeRow:
        """Record an instalment against a charge and return the updated charge.

        ``amount`` must be positive and must not exceed the remaining balance
        (no overpayment); ``method`` must be a :class:`PaymentMethod` name — all
        else :class:`SehatyValidationError`. The charge must belong to
        ``doctor_id``.
        """
        if amount <= 0:
            raise SehatyValidationError("amount must be positive")
        try:
            pay_method = PaymentMethod(method)
        except ValueError as exc:
            raise SehatyValidationError(
                f"invalid method {method!r}; expected one of {[m.value for m in PaymentMethod]}"
            ) from exc
        with get_session() as session:
            charge = _own_charge(session, doctor_id, charge_id)
            already = float(
                session.execute(
                    select(func.coalesce(func.sum(PatientPayment.amount), 0.0)).where(
                        PatientPayment.charge_id == charge_id
                    )
                ).scalar_one()
            )
            balance = round(float(charge.total_amount) - already, 2)
            if amount > balance + 1e-9:
                raise SehatyValidationError(
                    f"amount {amount} exceeds outstanding balance {balance}"
                )
            session.add(
                PatientPayment(
                    charge_id=charge_id,
                    amount=float(amount),
                    method=pay_method,
                    paid_at=paid_at or datetime.now(UTC),
                    note=note,
                    created_by=created_by,
                )
            )
            session.flush()
            session.refresh(charge)
            row = _charge_row(charge)
        return row

    @staticmethod
    def delete_payment(doctor_id: int, charge_id: int, payment_id: int) -> ChargeRow:
        """Remove a mis-entered payment and return the updated charge.

        Both the charge (by ``doctor_id``) and the payment (by ``charge_id``)
        must exist, else :class:`SehatyNotFoundError`.
        """
        with get_session() as session:
            charge = _own_charge(session, doctor_id, charge_id)
            payment = session.execute(
                select(PatientPayment).where(
                    PatientPayment.id == payment_id, PatientPayment.charge_id == charge_id
                )
            ).scalar_one_or_none()
            if payment is None:
                raise SehatyNotFoundError(f"no payment {payment_id} on charge {charge_id}")
            session.delete(payment)
            session.flush()
            session.refresh(charge)
            row = _charge_row(charge)
        return row

    @staticmethod
    def delete_charge(doctor_id: int, charge_id: int) -> None:
        """Remove a mis-entered charge (and, by cascade, its payments)."""
        with get_session() as session:
            charge = _own_charge(session, doctor_id, charge_id)
            session.delete(charge)

    @staticmethod
    def list_debtors(doctor_id: int, limit: int = 100) -> list[DebtorRow]:
        """Register patients who still owe money, biggest balance first.

        One grouped query: charges joined to their payments (left, so unpaid
        charges count) and to the register row for the display fields; rows with
        a zero/negative derived balance are filtered out after aggregation
        (portable HAVING on the computed sums).
        """
        paid = func.coalesce(func.sum(PatientPayment.amount), 0.0)
        charged = func.sum(PatientCharge.total_amount)

        # Payments must be summed per-charge first, else a charge with N
        # payments would multiply its total_amount N times in the join.
        per_charge_paid = (
            select(
                PatientPayment.charge_id.label("charge_id"),
                func.sum(PatientPayment.amount).label("paid"),
            )
            .group_by(PatientPayment.charge_id)
            .subquery()
        )
        paid = func.coalesce(func.sum(per_charge_paid.c.paid), 0.0)

        stmt = (
            select(
                PatientCharge.clinic_patient_id,
                ClinicPatient.full_name,
                ClinicPatient.phone,
                charged.label("total_charged"),
                paid.label("total_paid"),
            )
            .join(ClinicPatient, ClinicPatient.id == PatientCharge.clinic_patient_id)
            .outerjoin(per_charge_paid, per_charge_paid.c.charge_id == PatientCharge.id)
            .where(PatientCharge.doctor_id == doctor_id)
            .group_by(PatientCharge.clinic_patient_id, ClinicPatient.full_name, ClinicPatient.phone)
            .having(charged - paid > 0)
            .order_by((charged - paid).desc())
            .limit(limit)
        )
        with get_session() as session:
            rows = session.execute(stmt).all()
        return [
            DebtorRow(
                clinic_patient_id=r.clinic_patient_id,
                full_name=r.full_name,
                phone=r.phone,
                total_charged=round(float(r.total_charged), 2),
                total_paid=round(float(r.total_paid), 2),
                balance=round(float(r.total_charged) - float(r.total_paid), 2),
            )
            for r in rows
        ]
