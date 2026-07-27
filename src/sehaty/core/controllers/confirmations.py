"""Appointment confirmations and the secretary's day view.

What a doctor is actually buying: fewer empty slots. Detecting a no-show is only
half of that — the other half is refilling the slot, which is why releasing one
hands it to the waitlist (see ``waitlist.py``). Detection without refill is a
nicer way to watch money leave.

**Timing.** The confirmation is asked at T-24h, not an hour ahead. An hour ahead
you learn the slot is already lost; a day ahead the secretary can still sell it.

**Scoring.** Four rules, not a model. There is no history to train on, and a
secretary will not act on a number she cannot argue with. Explainability beats
accuracy here; swap in something learned once there are thousands of visits.

**Privacy.** The message text never names the specialty. "Rendez-vous avec le
Dr X, psychiatre" landing on a shared family phone is a health-data disclosure
about the patient (Law 09-08); the cabinet name carries the same information for
the patient and none for anyone else.
"""

from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote

from sehaty.db import (
    Appointment,
    AppointmentStatus,
    Cabinet,
    ConfirmationChannel,
    ConfirmationStatus,
    OutboundMessage,
    OutboundStatus,
    PatientProfile,
    User,
)
from sqlalchemy import Integer, func, or_, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError

# When the confirmation is asked, and when silence becomes a red flag.
CONFIRM_LEAD_HOURS = 24
CHASE_AFTER_HOURS = 6  # i.e. T-18h, six hours after the T-24h ask
REMINDER_LEAD_HOURS = 2

_SCORE_MAX = 4


class DayRow(DomainModel):
    """One appointment as the secretary sees it."""

    appointment_id: int
    start_at: datetime
    end_at: datetime
    patient_name: str
    patient_phone: str | None
    status: str
    confirmation_status: str
    confirmation_sent_at: datetime | None
    no_show_score: int
    # Why the score is what it is — shown in the UI so the secretary can judge
    # the reasoning rather than trust a bare number.
    score_reasons: list[str]
    # Pre-filled wa.me link the secretary taps to send the ask herself. This is
    # the v1 channel: no Meta verification, no per-message cost, ~85% of the
    # value of full automation.
    whatsapp_url: str | None


class DaySummary(DomainModel):
    """A doctor's day, plus the counts the secretary triages on."""

    day: date
    rows: list[DayRow]
    confirmed: int
    at_risk: int
    unreachable: int


def _as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive timestamp.

    Postgres round-trips ``DateTime(timezone=True)`` as aware, SQLite does not.
    Normalizing on read keeps the arithmetic below correct on both rather than
    raising "can't subtract offset-naive and offset-aware datetimes".
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _score(
    *,
    is_first_visit: bool,
    prior_no_shows: int,
    booked_days_ahead: int,
    confirmation: ConfirmationStatus,
    asked_hours_ago: float | None,
) -> tuple[int, list[str]]:
    """Rule-based no-show risk, 0-4, with the reasons that produced it."""
    score = 0
    reasons: list[str] = []

    if is_first_visit:
        score += 1
        reasons.append("premier rendez-vous")
    if prior_no_shows > 0:
        score += 1
        reasons.append(f"{prior_no_shows} absence(s) précédente(s)")
    if booked_days_ahead > 7:
        score += 1
        reasons.append("réservé il y a plus de 7 jours")

    if confirmation == ConfirmationStatus.CONFIRMED:
        # Confirming is the strongest signal available, so it outweighs one
        # risk factor rather than merely cancelling it out.
        score -= 2
        reasons.append("a confirmé")
    elif confirmation == ConfirmationStatus.DECLINED:
        score = _SCORE_MAX
        reasons.append("a annulé")
    elif asked_hours_ago is not None and asked_hours_ago >= CHASE_AFTER_HOURS:
        score += 1
        reasons.append("pas de réponse")

    return max(0, min(_SCORE_MAX, score)), reasons


def whatsapp_ask_url(phone: str | None, patient: str, cabinet: str, when: datetime) -> str | None:
    """A pre-filled `wa.me` link for the secretary to tap.

    The text names the cabinet, never the specialty — see the module docstring.
    """
    if not phone or not phone.strip():
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = f"212{digits[1:]}"
    if not digits:
        return None

    text = (
        f"Bonjour {patient}, vous avez un rendez-vous demain à "
        f"{when.strftime('%H:%M')} au {cabinet}. "
        "Merci de répondre OUI pour confirmer ou NON pour annuler."
    )
    return f"https://wa.me/{digits}?text={quote(text)}"


class ConfirmationController:
    @staticmethod
    def day_view(doctor_id: int, day: date, *, now: datetime | None = None) -> DaySummary:
        """Every appointment for ``doctor_id`` on ``day``, scored and triaged."""
        now = now or datetime.now(UTC)
        start = datetime.combine(day, time.min, tzinfo=UTC)
        end = start + timedelta(days=1)

        with get_session() as session:
            cabinet_name = (
                session.execute(
                    select(Cabinet.name).where(Cabinet.owner_doctor_id == doctor_id).limit(1)
                ).scalar_one_or_none()
                or "cabinet"
            )

            rows = session.execute(
                select(
                    Appointment.id,
                    Appointment.patient_id,
                    Appointment.start_at,
                    Appointment.end_at,
                    Appointment.status,
                    Appointment.confirmation_status,
                    Appointment.confirmation_sent_at,
                    Appointment.created_at,
                    PatientProfile.full_name,
                    User.phone,
                )
                .join(User, User.id == Appointment.patient_id)
                .outerjoin(PatientProfile, PatientProfile.user_id == Appointment.patient_id)
                .where(
                    Appointment.doctor_id == doctor_id,
                    Appointment.start_at >= start,
                    Appointment.start_at < end,
                    Appointment.status.notin_(
                        [AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED]
                    ),
                )
                .order_by(Appointment.start_at.asc())
            ).all()

            patient_ids = [row.patient_id for row in rows]
            history = _patient_history(session, doctor_id, patient_ids)

        day_rows: list[DayRow] = []
        for row in rows:
            completed, no_shows = history.get(row.patient_id, (0, 0))
            sent_at = _as_utc(row.confirmation_sent_at)
            asked_hours = (now - sent_at).total_seconds() / 3600 if sent_at else None
            created_at = _as_utc(row.created_at)
            start_at = _as_utc(row.start_at)
            booked_days_ahead = (start_at - created_at).days if created_at and start_at else 0
            score, reasons = _score(
                is_first_visit=completed == 0,
                prior_no_shows=no_shows,
                booked_days_ahead=booked_days_ahead,
                confirmation=row.confirmation_status,
                asked_hours_ago=asked_hours,
            )
            name = row.full_name or "Patient"
            day_rows.append(
                DayRow(
                    appointment_id=row.id,
                    start_at=row.start_at,
                    end_at=row.end_at,
                    patient_name=name,
                    patient_phone=row.phone,
                    status=str(row.status),
                    confirmation_status=str(row.confirmation_status),
                    confirmation_sent_at=row.confirmation_sent_at,
                    no_show_score=score,
                    score_reasons=reasons,
                    whatsapp_url=whatsapp_ask_url(row.phone, name, cabinet_name, row.start_at),
                )
            )

        confirmed = sum(
            1 for r in day_rows if r.confirmation_status == ConfirmationStatus.CONFIRMED
        )
        at_risk = sum(1 for r in day_rows if r.no_show_score >= 3)
        unreachable = sum(1 for r in day_rows if not r.patient_phone)
        return DaySummary(
            day=day,
            rows=day_rows,
            confirmed=confirmed,
            at_risk=at_risk,
            unreachable=unreachable,
        )

    @staticmethod
    def mark_sent(
        appointment_id: int,
        *,
        channel: ConfirmationChannel = ConfirmationChannel.WHATSAPP_MANUAL,
        template: str | None = None,
        provider_message_id: str | None = None,
    ) -> None:
        """Record that the confirmation ask went out, and log the message.

        ``confirmation_sent_at`` is only stamped the first time: the T-18h chase
        window is measured from the original ask, so re-sending must not reset
        the clock and hide a patient who has been silent all day.
        """
        with get_session() as session:
            appointment = session.get(Appointment, appointment_id)
            if appointment is None:
                raise SehatyNotFoundError(f"no appointment {appointment_id}")

            if appointment.confirmation_sent_at is None:
                appointment.confirmation_sent_at = datetime.now(UTC)
            appointment.confirmation_channel = channel
            session.add(
                OutboundMessage(
                    appointment_id=appointment_id,
                    channel=channel,
                    template=template,
                    status=OutboundStatus.SENT,
                    provider_message_id=provider_message_id,
                )
            )

    @staticmethod
    def record_reply(appointment_id: int, *, confirmed: bool) -> str:
        """Record the patient's answer. Returns the new confirmation status."""
        with get_session() as session:
            appointment = session.get(Appointment, appointment_id)
            if appointment is None:
                raise SehatyNotFoundError(f"no appointment {appointment_id}")

            appointment.confirmation_status = (
                ConfirmationStatus.CONFIRMED if confirmed else ConfirmationStatus.DECLINED
            )
            appointment.confirmation_replied_at = datetime.now(UTC)
            if confirmed:
                # A confirmed request is a booking the doctor can rely on.
                if appointment.status == AppointmentStatus.REQUESTED:
                    appointment.status = AppointmentStatus.CONFIRMED
            else:
                # Declining frees the slot straight away — that is the point.
                appointment.status = AppointmentStatus.CANCELLED
            return str(appointment.confirmation_status)

    @staticmethod
    def appointment_awaiting_reply(wa_id: str | None, *, now: datetime | None = None) -> int | None:
        """Which appointment an inbound WhatsApp reply is answering.

        A reply carries only the sender's number, so it has to be matched back
        to a visit. Resolution is deliberately narrow: the patient's *soonest
        upcoming* appointment that was actually asked and has not yet answered.

        Returns None rather than guessing when nothing matches. A patient with
        two pending asks would otherwise have an arbitrary one resolved by a
        single "oui", and cancelling the wrong visit is worse than leaving both
        for the secretary.
        """
        if not wa_id:
            return None
        now = now or datetime.now(UTC)
        digits = "".join(c for c in wa_id if c.isdigit())
        if not digits:
            return None

        # Match the stored phone by its digits, however it happens to be
        # formatted (+212 6 61 ..., 0661..., 00212661...).
        local = f"0{digits[3:]}" if digits.startswith("212") else digits

        with get_session() as session:
            patient_ids = [
                row.id
                for row in session.execute(
                    select(User.id, User.phone).where(User.phone.is_not(None))
                ).all()
                if _phone_digits(row.phone) in {digits, local, f"212{local[1:]}"}
            ]
            if not patient_ids:
                return None

            return session.execute(
                select(Appointment.id)
                .where(
                    Appointment.patient_id.in_(patient_ids),
                    Appointment.start_at > now,
                    Appointment.confirmation_sent_at.is_not(None),
                    Appointment.confirmation_status.in_(
                        [ConfirmationStatus.PENDING, ConfirmationStatus.NO_REPLY]
                    ),
                )
                .order_by(Appointment.start_at.asc())
                .limit(1)
            ).scalar_one_or_none()

    @staticmethod
    def due_for_confirmation(*, now: datetime | None = None, limit: int = 200) -> list[int]:
        """Appointment ids that should be asked to confirm now (T-24h).

        Excludes anything already asked, so the job is idempotent and a patient
        is never messaged twice for one visit.
        """
        if limit <= 0 or limit > 1000:
            raise SehatyValidationError(f"limit out of range: {limit}")
        now = now or datetime.now(UTC)
        horizon = now + timedelta(hours=CONFIRM_LEAD_HOURS)

        with get_session() as session:
            return list(
                session.execute(
                    select(Appointment.id)
                    .where(
                        Appointment.confirmation_sent_at.is_(None),
                        Appointment.start_at > now,
                        Appointment.start_at <= horizon,
                        Appointment.status.in_(
                            [AppointmentStatus.REQUESTED, AppointmentStatus.CONFIRMED]
                        ),
                    )
                    .order_by(Appointment.start_at.asc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )

    @staticmethod
    def expire_silent(*, now: datetime | None = None) -> int:
        """Flip long-unanswered asks to NO_REPLY. Returns how many changed.

        Silence after a direct question is a different state from never having
        been asked, and the secretary triages on that difference.
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(hours=CHASE_AFTER_HOURS)

        with get_session() as session:
            appointments = (
                session.execute(
                    select(Appointment).where(
                        Appointment.confirmation_status == ConfirmationStatus.PENDING,
                        Appointment.confirmation_sent_at.is_not(None),
                        Appointment.confirmation_sent_at <= cutoff,
                        Appointment.start_at > now,
                    )
                )
                .scalars()
                .all()
            )
            for appointment in appointments:
                appointment.confirmation_status = ConfirmationStatus.NO_REPLY
            return len(appointments)


def _phone_digits(phone: str | None) -> str:
    """Digits only, so stored formats compare regardless of punctuation."""
    return "".join(c for c in (phone or "") if c.isdigit())


def _patient_history(session, doctor_id: int, patient_ids: list[int]):  # noqa: ANN001
    """``patient_id -> (completed_visits, no_shows)`` with this doctor, in one query."""
    if not patient_ids:
        return {}

    rows = session.execute(
        select(
            Appointment.patient_id,
            func.sum(func.cast(Appointment.status == AppointmentStatus.COMPLETED, Integer)).label(
                "completed"
            ),
            func.sum(func.cast(Appointment.status == AppointmentStatus.NO_SHOW, Integer)).label(
                "no_shows"
            ),
        )
        .where(
            Appointment.doctor_id == doctor_id,
            Appointment.patient_id.in_(patient_ids),
            or_(
                Appointment.status == AppointmentStatus.COMPLETED,
                Appointment.status == AppointmentStatus.NO_SHOW,
            ),
        )
        .group_by(Appointment.patient_id)
    ).all()
    return {row.patient_id: (int(row.completed or 0), int(row.no_shows or 0)) for row in rows}
