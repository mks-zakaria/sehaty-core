"""Pure-SQLite tests for confirmations, no-show scoring and the waitlist.

The properties worth pinning are the ones that decide whether the feature earns
its 199 DH: the ask goes out with enough lead time to resell the slot, silence
is distinguishable from never-asked, releasing a slot actually offers it to
somebody, and the queue never stalls on one unanswered offer.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sehaty.db import (
    Appointment,
    AppointmentStatus,
    Cabinet,
    ConfirmationStatus,
    OutboundMessage,
    PatientProfile,
    User,
    UserRole,
    WaitlistEntry,
    WaitlistStatus,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.confirmations import (
    CHASE_AFTER_HOURS,
    ConfirmationController,
    whatsapp_ask_url,
)
from sehaty.core.controllers.waitlist import (
    OFFER_TTL_MINUTES,
    WaitlistController,
)
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyConflictError

_TABLES = [
    User.__table__,
    # The day view reads the cabinet name for the message text.
    Cabinet.__table__,
    PatientProfile.__table__,
    Appointment.__table__,
    OutboundMessage.__table__,
    WaitlistEntry.__table__,
]

NOW = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)


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


def _user(factory: sessionmaker[Session], email: str, role: UserRole, phone=None) -> int:
    with factory() as s:
        user = User(email=email, role=role, is_active=True, phone=phone)
        s.add(user)
        s.flush()
        if role == UserRole.PATIENT:
            s.add(PatientProfile(user_id=user.id, full_name=email.split("@")[0]))
        s.commit()
        return int(user.id)


def _appointment(
    factory: sessionmaker[Session],
    doctor_id: int,
    patient_id: int,
    *,
    start_at: datetime,
    status: AppointmentStatus = AppointmentStatus.CONFIRMED,
    confirmation_sent_at: datetime | None = None,
    confirmation_status: ConfirmationStatus = ConfirmationStatus.PENDING,
    created_at: datetime | None = None,
) -> int:
    with factory() as s:
        appointment = Appointment(
            doctor_id=doctor_id,
            patient_id=patient_id,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=status,
            confirmation_sent_at=confirmation_sent_at,
            confirmation_status=confirmation_status,
        )
        if created_at:
            appointment.created_at = created_at
        s.add(appointment)
        s.flush()
        s.commit()
        return int(appointment.id)


class TestWhatsappAskUrl:
    def test_normalizes_a_local_number(self) -> None:
        url = whatsapp_ask_url("0661234567", "Amina", "Cabinet Maârif", NOW)
        assert url.startswith("https://wa.me/212661234567?text=")

    def test_never_names_the_specialty(self) -> None:
        # A message naming "psychiatre" on a shared family phone discloses the
        # patient's condition to whoever picks up the handset.
        url = whatsapp_ask_url("0661234567", "Amina", "Cabinet Maârif", NOW)
        assert "psychiatre" not in url.lower()
        assert "Cabinet%20Ma" in url

    def test_returns_none_without_a_number(self) -> None:
        assert whatsapp_ask_url(None, "Amina", "Cabinet", NOW) is None
        assert whatsapp_ask_url("   ", "Amina", "Cabinet", NOW) is None


class TestDueForConfirmation:
    def test_picks_up_appointments_a_day_out(self, db: sessionmaker[Session]) -> None:
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "pat@c.ma", UserRole.PATIENT, phone="+212661234567")
        soon = _appointment(db, doctor, patient, start_at=NOW + timedelta(hours=20))

        assert ConfirmationController.due_for_confirmation(now=NOW) == [soon]

    def test_ignores_appointments_beyond_the_lead_window(self, db: sessionmaker[Session]) -> None:
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "pat@c.ma", UserRole.PATIENT)
        _appointment(db, doctor, patient, start_at=NOW + timedelta(days=5))

        assert ConfirmationController.due_for_confirmation(now=NOW) == []

    def test_is_idempotent_once_asked(self, db: sessionmaker[Session]) -> None:
        # A patient messaged twice for one visit is a support call, not a nudge.
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "pat@c.ma", UserRole.PATIENT)
        appointment = _appointment(db, doctor, patient, start_at=NOW + timedelta(hours=20))

        ConfirmationController.mark_sent(appointment)

        assert ConfirmationController.due_for_confirmation(now=NOW) == []

    def test_skips_cancelled_appointments(self, db: sessionmaker[Session]) -> None:
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "pat@c.ma", UserRole.PATIENT)
        _appointment(
            db,
            doctor,
            patient,
            start_at=NOW + timedelta(hours=20),
            status=AppointmentStatus.CANCELLED,
        )
        assert ConfirmationController.due_for_confirmation(now=NOW) == []


class TestMarkSentAndReply:
    def test_logs_the_outbound_message(self, db: sessionmaker[Session]) -> None:
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "pat@c.ma", UserRole.PATIENT)
        appointment = _appointment(db, doctor, patient, start_at=NOW + timedelta(hours=20))

        ConfirmationController.mark_sent(appointment)

        with db() as s:
            assert s.execute(select(OutboundMessage)).scalar_one().appointment_id == appointment

    def test_resending_does_not_reset_the_chase_clock(self, db: sessionmaker[Session]) -> None:
        # Otherwise a patient silent all day looks freshly asked every time the
        # secretary re-sends, and never turns red.
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "pat@c.ma", UserRole.PATIENT)
        appointment = _appointment(db, doctor, patient, start_at=NOW + timedelta(hours=20))

        ConfirmationController.mark_sent(appointment)
        with db() as s:
            first = s.get(Appointment, appointment).confirmation_sent_at
        ConfirmationController.mark_sent(appointment)
        with db() as s:
            assert s.get(Appointment, appointment).confirmation_sent_at == first

    def test_confirming_promotes_a_requested_appointment(self, db: sessionmaker[Session]) -> None:
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "pat@c.ma", UserRole.PATIENT)
        appointment = _appointment(
            db,
            doctor,
            patient,
            start_at=NOW + timedelta(hours=20),
            status=AppointmentStatus.REQUESTED,
        )

        ConfirmationController.record_reply(appointment, confirmed=True)

        with db() as s:
            row = s.get(Appointment, appointment)
        assert row.confirmation_status == ConfirmationStatus.CONFIRMED
        assert row.status == AppointmentStatus.CONFIRMED

    def test_declining_frees_the_slot_immediately(self, db: sessionmaker[Session]) -> None:
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "pat@c.ma", UserRole.PATIENT)
        appointment = _appointment(db, doctor, patient, start_at=NOW + timedelta(hours=20))

        ConfirmationController.record_reply(appointment, confirmed=False)

        with db() as s:
            row = s.get(Appointment, appointment)
        assert row.confirmation_status == ConfirmationStatus.DECLINED
        assert row.status == AppointmentStatus.CANCELLED


class TestExpireSilent:
    def test_silence_becomes_no_reply(self, db: sessionmaker[Session]) -> None:
        # NO_REPLY is not PENDING: silence after a direct question is itself a
        # signal, and the secretary triages on the difference.
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "pat@c.ma", UserRole.PATIENT)
        appointment = _appointment(
            db,
            doctor,
            patient,
            start_at=NOW + timedelta(hours=18),
            confirmation_sent_at=NOW - timedelta(hours=CHASE_AFTER_HOURS + 1),
        )

        assert ConfirmationController.expire_silent(now=NOW) == 1
        with db() as s:
            assert s.get(Appointment, appointment).confirmation_status == (
                ConfirmationStatus.NO_REPLY
            )

    def test_a_recent_ask_is_left_alone(self, db: sessionmaker[Session]) -> None:
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "pat@c.ma", UserRole.PATIENT)
        _appointment(
            db,
            doctor,
            patient,
            start_at=NOW + timedelta(hours=20),
            confirmation_sent_at=NOW - timedelta(hours=1),
        )
        assert ConfirmationController.expire_silent(now=NOW) == 0


class TestDayViewScoring:
    def _setup(self, db: sessionmaker[Session], **kwargs) -> tuple[int, int]:
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "pat@c.ma", UserRole.PATIENT, phone="+212661234567")
        _appointment(db, doctor, patient, start_at=NOW + timedelta(hours=4), **kwargs)
        return doctor, patient

    def test_a_first_time_patient_scores_one(self, db: sessionmaker[Session]) -> None:
        doctor, _ = self._setup(db)
        summary = ConfirmationController.day_view(doctor, NOW.date(), now=NOW)
        row = summary.rows[0]
        assert row.no_show_score == 1
        assert "premier rendez-vous" in row.score_reasons

    def test_confirming_outweighs_a_risk_factor(self, db: sessionmaker[Session]) -> None:
        doctor, _ = self._setup(db, confirmation_status=ConfirmationStatus.CONFIRMED)
        row = ConfirmationController.day_view(doctor, NOW.date(), now=NOW).rows[0]
        assert row.no_show_score == 0
        assert "a confirmé" in row.score_reasons

    def test_silence_after_the_chase_window_adds_risk(self, db: sessionmaker[Session]) -> None:
        doctor, _ = self._setup(
            db, confirmation_sent_at=NOW - timedelta(hours=CHASE_AFTER_HOURS + 1)
        )
        row = ConfirmationController.day_view(doctor, NOW.date(), now=NOW).rows[0]
        assert "pas de réponse" in row.score_reasons
        assert row.no_show_score == 2

    def test_a_prior_no_show_adds_risk(self, db: sessionmaker[Session]) -> None:
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "pat@c.ma", UserRole.PATIENT, phone="+212661234567")
        _appointment(
            db,
            doctor,
            patient,
            start_at=NOW - timedelta(days=30),
            status=AppointmentStatus.NO_SHOW,
        )
        _appointment(
            db,
            doctor,
            patient,
            start_at=NOW - timedelta(days=60),
            status=AppointmentStatus.COMPLETED,
        )
        _appointment(db, doctor, patient, start_at=NOW + timedelta(hours=4))

        row = ConfirmationController.day_view(doctor, NOW.date(), now=NOW).rows[0]
        # Not a first visit any more, but has an absence on record.
        assert "premier rendez-vous" not in row.score_reasons
        assert any("absence" in reason for reason in row.score_reasons)

    def test_every_row_carries_a_tap_to_send_link(self, db: sessionmaker[Session]) -> None:
        doctor, _ = self._setup(db)
        row = ConfirmationController.day_view(doctor, NOW.date(), now=NOW).rows[0]
        assert row.whatsapp_url and row.whatsapp_url.startswith("https://wa.me/")

    def test_counts_summarize_the_day(self, db: sessionmaker[Session]) -> None:
        doctor, _ = self._setup(db, confirmation_status=ConfirmationStatus.CONFIRMED)
        summary = ConfirmationController.day_view(doctor, NOW.date(), now=NOW)
        assert summary.confirmed == 1
        assert summary.at_risk == 0

    def test_a_patient_with_no_phone_is_flagged_unreachable(
        self, db: sessionmaker[Session]
    ) -> None:
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        patient = _user(db, "nophone@c.ma", UserRole.PATIENT)
        _appointment(db, doctor, patient, start_at=NOW + timedelta(hours=4))

        summary = ConfirmationController.day_view(doctor, NOW.date(), now=NOW)
        assert summary.unreachable == 1
        assert summary.rows[0].whatsapp_url is None


class TestWaitlist:
    def _pair(self, db: sessionmaker[Session]) -> tuple[int, int, int]:
        doctor = _user(db, "doc@c.ma", UserRole.DOCTOR)
        booked = _user(db, "booked@c.ma", UserRole.PATIENT, phone="+212661111111")
        waiting = _user(db, "waiting@c.ma", UserRole.PATIENT, phone="+212662222222")
        return doctor, booked, waiting

    def test_join_then_appear_in_the_queue(self, db: sessionmaker[Session]) -> None:
        doctor, _, waiting = self._pair(db)
        entry_id = WaitlistController.join(doctor, waiting)

        queue = WaitlistController.queue(doctor)
        assert [row.entry_id for row in queue] == [entry_id]

    def test_joining_twice_is_a_conflict_not_a_duplicate(self, db: sessionmaker[Session]) -> None:
        # Duplicate entries would quietly double someone's odds.
        doctor, _, waiting = self._pair(db)
        WaitlistController.join(doctor, waiting)
        with pytest.raises(SehatyConflictError):
            WaitlistController.join(doctor, waiting)

    def test_releasing_a_slot_offers_it_to_the_next_patient(
        self, db: sessionmaker[Session]
    ) -> None:
        # The whole economic argument: detection is worthless without refill.
        doctor, booked, waiting = self._pair(db)
        WaitlistController.join(doctor, waiting)
        appointment = _appointment(db, doctor, booked, start_at=NOW + timedelta(hours=4))

        result = WaitlistController.release_slot(appointment, now=NOW)

        assert result.offered is True
        assert result.offered_to.patient_id == waiting
        with db() as s:
            assert s.get(Appointment, appointment).status == AppointmentStatus.CANCELLED

    def test_releasing_with_an_empty_queue_still_frees_the_slot(
        self, db: sessionmaker[Session]
    ) -> None:
        doctor, booked, _ = self._pair(db)
        appointment = _appointment(db, doctor, booked, start_at=NOW + timedelta(hours=4))

        result = WaitlistController.release_slot(appointment, now=NOW)

        assert result.offered is False
        with db() as s:
            assert s.get(Appointment, appointment).status == AppointmentStatus.CANCELLED

    def test_offers_go_first_come_first_served(self, db: sessionmaker[Session]) -> None:
        doctor, booked, first = self._pair(db)
        second = _user(db, "second@c.ma", UserRole.PATIENT)
        WaitlistController.join(doctor, first)
        WaitlistController.join(doctor, second)
        appointment = _appointment(db, doctor, booked, start_at=NOW + timedelta(hours=4))

        result = WaitlistController.release_slot(appointment, now=NOW)
        assert result.offered_to.patient_id == first

    def test_never_offers_the_slot_back_to_the_patient_who_lost_it(
        self, db: sessionmaker[Session]
    ) -> None:
        doctor, booked, _ = self._pair(db)
        WaitlistController.join(doctor, booked)
        appointment = _appointment(db, doctor, booked, start_at=NOW + timedelta(hours=4))

        assert WaitlistController.release_slot(appointment, now=NOW).offered is False

    def test_respects_the_patients_date_window(self, db: sessionmaker[Session]) -> None:
        doctor, booked, waiting = self._pair(db)
        WaitlistController.join(doctor, waiting, earliest_at=NOW + timedelta(days=10))
        appointment = _appointment(db, doctor, booked, start_at=NOW + timedelta(hours=4))

        assert WaitlistController.release_slot(appointment, now=NOW).offered is False

    def test_accepting_reassigns_the_existing_slot(self, db: sessionmaker[Session]) -> None:
        # Reassigned rather than duplicated, so the slot cannot be double-booked.
        doctor, booked, waiting = self._pair(db)
        entry_id = WaitlistController.join(doctor, waiting)
        appointment = _appointment(db, doctor, booked, start_at=NOW + timedelta(hours=4))
        WaitlistController.release_slot(appointment, now=NOW)

        taken = WaitlistController.accept_offer(entry_id, waiting)

        assert taken == appointment
        with db() as s:
            row = s.get(Appointment, appointment)
            assert row.patient_id == waiting
            assert row.status == AppointmentStatus.CONFIRMED
            assert len(s.execute(select(Appointment)).scalars().all()) == 1

    def test_declining_returns_the_patient_to_the_queue(self, db: sessionmaker[Session]) -> None:
        doctor, booked, waiting = self._pair(db)
        entry_id = WaitlistController.join(doctor, waiting)
        appointment = _appointment(db, doctor, booked, start_at=NOW + timedelta(hours=4))
        WaitlistController.release_slot(appointment, now=NOW)

        WaitlistController.decline_offer(entry_id, waiting)

        with db() as s:
            assert s.get(WaitlistEntry, entry_id).status == WaitlistStatus.WAITING

    def test_a_stale_offer_does_not_block_the_queue(self, db: sessionmaker[Session]) -> None:
        # One slow reply must not hold a slot empty indefinitely — that is the
        # exact failure the waitlist exists to prevent.
        doctor, booked, first = self._pair(db)
        second = _user(db, "second@c.ma", UserRole.PATIENT)
        WaitlistController.join(doctor, first)
        WaitlistController.join(doctor, second)

        one = _appointment(db, doctor, booked, start_at=NOW + timedelta(hours=4))
        assert WaitlistController.release_slot(one, now=NOW).offered_to.patient_id == first

        later = NOW + timedelta(minutes=OFFER_TTL_MINUTES + 1)
        two = _appointment(db, doctor, booked, start_at=later + timedelta(hours=4))
        result = WaitlistController.release_slot(two, now=later)

        # The first patient timed out and went back to WAITING, so they are
        # eligible again — and crucially the queue moved at all.
        assert result.offered is True

    def test_cannot_accept_a_slot_someone_else_took(self, db: sessionmaker[Session]) -> None:
        doctor, booked, waiting = self._pair(db)
        entry_id = WaitlistController.join(doctor, waiting)
        appointment = _appointment(db, doctor, booked, start_at=NOW + timedelta(hours=4))
        WaitlistController.release_slot(appointment, now=NOW)

        with db() as s:
            s.get(Appointment, appointment).status = AppointmentStatus.CONFIRMED
            s.commit()

        with pytest.raises(SehatyConflictError):
            WaitlistController.accept_offer(entry_id, waiting)
