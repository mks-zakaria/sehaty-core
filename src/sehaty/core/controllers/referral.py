"""Doctor-referral business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). Every
doctor carries a short ``referral_code``. A new doctor may register with a
referrer's code; that link is recorded as a ``PENDING`` ``Referral``. When the
referred doctor pays their *first* cash invoice the referrer earns a
subscription credit — an admin-configurable amount (``referral_reward_mad``,
default 199 MAD) posted to the append-only ``CreditLedger`` via
``BillingController.apply_credit``. Settlement is idempotent: only a ``PENDING``
referral is ever rewarded, so replaying a receipt never double-credits.

Reads/writes over ``DoctorProfile`` use column-only projections and bulk
``UPDATE`` so the PostGIS ``geopoint`` blob is never loaded (stock SQLite cannot
compile it). Failures raise the ``SehatyError`` taxonomy.
"""

import json
import secrets
from datetime import datetime

from sehaty.db import (
    AdminConfig,
    DoctorProfile,
    Referral,
    ReferralStatus,
)
from sqlalchemy import select, update

from sehaty.core._dto import DomainModel
from sehaty.core.controllers.billing import BillingController
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError

# Admin-config key + fallback for the reward posted to a referrer.
_REWARD_KEY = "referral_reward_mad"
_DEFAULT_REWARD = 199.0
# Referral-code shape: unambiguous uppercase alphabet, fixed length.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8


class ReferralRow(DomainModel):
    """A single referral this doctor made (detached projection).

    The plain, id-based view returned by
    :meth:`ReferralController.list_for_referrer` — the transport layer
    serialises it directly.
    """

    id: int
    referred_doctor_id: int | None
    status: str
    reward_amount: float | None
    created_at: datetime


class ReferralController:
    @staticmethod
    def code_for(doctor_id: int) -> str:
        """Return the doctor's ``referral_code``, minting one if absent.

        Column-only projection (never loads ``geopoint``). A doctor with no
        profile row raises ``SehatyNotFoundError``. The generated code is short,
        unambiguous, and unique across ``doctor_profiles``; it is persisted with
        a bulk ``UPDATE`` and is stable across later calls.
        """
        with get_session() as session:
            row = session.execute(
                select(DoctorProfile.user_id, DoctorProfile.referral_code).where(
                    DoctorProfile.user_id == doctor_id
                )
            ).one_or_none()
            if row is None:
                raise SehatyNotFoundError(f"no doctor profile for user {doctor_id}")
            if row.referral_code:
                return row.referral_code

            code = ReferralController._generate_code(session)
            session.execute(
                update(DoctorProfile)
                .where(DoctorProfile.user_id == doctor_id)
                .values(referral_code=code)
            )
            return code

    @staticmethod
    def resolve_referrer(code: str) -> int | None:
        """Return the doctor_id owning ``code``, or ``None`` if unknown."""
        if not code:
            return None
        with get_session() as session:
            return session.execute(
                select(DoctorProfile.user_id).where(DoctorProfile.referral_code == code)
            ).scalar_one_or_none()

    @staticmethod
    def record_referral(referrer_code: str, referred_doctor_id: int) -> Referral | None:
        """Link a referrer (by code) to a newly registered referred doctor.

        Creates a ``PENDING`` ``Referral`` iff the code resolves to a *different*
        doctor and no referral already exists for ``referred_doctor_id``.
        Idempotent and self-referral-safe: an unknown code, a self-referral, or
        a referred doctor who is already claimed all return ``None``.
        """
        referrer_id = ReferralController.resolve_referrer(referrer_code)
        if referrer_id is None or referrer_id == referred_doctor_id:
            return None

        with get_session() as session:
            existing = session.execute(
                select(Referral).where(Referral.referred_doctor_id == referred_doctor_id)
            ).scalar_one_or_none()
            if existing is not None:
                return None

            referral = Referral(
                referrer_doctor_id=referrer_id,
                referred_doctor_id=referred_doctor_id,
                code=referrer_code,
                status=ReferralStatus.PENDING,
            )
            session.add(referral)
            session.flush()
            return referral

    @staticmethod
    def reward_amount() -> float:
        """Return the configured referral reward (``referral_reward_mad``).

        Falls back to ``199.0`` when the key is unset or unparseable.
        """
        with get_session() as session:
            raw = session.execute(
                select(AdminConfig.value).where(AdminConfig.key == _REWARD_KEY)
            ).scalar_one_or_none()
        if raw is None:
            return _DEFAULT_REWARD
        try:
            return float(json.loads(raw))
        except (ValueError, TypeError):
            try:
                return float(raw)
            except (ValueError, TypeError):
                return _DEFAULT_REWARD

    @staticmethod
    def settle_first_payment(referred_doctor_id: int) -> float:
        """Reward the referrer when their referred doctor's first invoice pays.

        If a ``PENDING`` referral exists for ``referred_doctor_id`` it is flipped
        ``REWARDED`` (stamping ``reward_amount``) *before* any credit is posted,
        which closes the double-reward window. The referrer then receives a
        ``CreditLedger`` credit via ``BillingController.apply_credit``. Returns
        the credit applied, or ``0.0`` when there is nothing to settle — so a
        second call (an already-``REWARDED`` referral) is a harmless no-op.
        """
        amount = ReferralController.reward_amount()
        with get_session() as session:
            referral = session.execute(
                select(Referral).where(
                    Referral.referred_doctor_id == referred_doctor_id,
                    Referral.status == ReferralStatus.PENDING,
                )
            ).scalar_one_or_none()
            if referral is None:
                return 0.0
            referrer_id = referral.referrer_doctor_id
            referral_id = referral.id
            referral.status = ReferralStatus.REWARDED
            referral.reward_amount = amount
            session.flush()

        ledger = BillingController.apply_credit(
            referrer_id,
            amount,
            reason="referral",
            ref_type="referral",
            ref_id=referral_id,
        )
        with get_session() as session:
            session.execute(
                update(Referral)
                .where(Referral.id == referral_id)
                .values(credit_ledger_id=ledger.id)
            )

        # Notify the referrer of the reward AFTER settlement commits (its own
        # session, never nested). Only reached when a PENDING referral was
        # actually rewarded (the no-op path returns 0.0 above), so a replayed
        # receipt never re-notifies. A notification failure must NEVER break
        # settlement, which is already persisted above.
        try:
            from sehaty.core.controllers.notifications import NotificationController

            NotificationController.notify(
                referrer_id,
                kind="referral_rewarded",
                message=f"You earned a {amount:.0f} MAD referral reward",
                entity="referral",
                entity_id=referral_id,
            )
        except Exception:
            pass
        return amount

    @staticmethod
    def list_for_referrer(doctor_id: int) -> list[ReferralRow]:
        """Return every referral this doctor made, oldest first (for the UI)."""
        with get_session() as session:
            return [
                ReferralRow.model_validate(r)
                for r in session.execute(
                    select(Referral)
                    .where(Referral.referrer_doctor_id == doctor_id)
                    .order_by(Referral.id)
                )
                .scalars()
                .all()
            ]

    @staticmethod
    def _generate_code(session) -> str:
        """Mint a short code unique across ``doctor_profiles.referral_code``."""
        taken = set(
            session.execute(
                select(DoctorProfile.referral_code).where(DoctorProfile.referral_code.is_not(None))
            )
            .scalars()
            .all()
        )
        while True:
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
            if code not in taken:
                return code
