"""Tests for the WhatsApp Cloud API service layer.

Pure functions, no database. Two things matter here and both are about not
guessing: an ambiguous reply must stay ambiguous rather than cancel someone's
appointment, and a malformed webhook must yield nothing rather than raise —
Meta retries non-2xx aggressively and eventually disables the endpoint.
"""

from datetime import UTC, datetime

import pytest

from sehaty.core.services.whatsapp import (
    build_confirmation_payload,
    interpret_reply,
    load_config,
    parse_inbound,
    to_wa_id,
)

WHEN = datetime(2026, 7, 28, 15, 30, tzinfo=UTC)


class TestConfig:
    def test_is_disabled_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A half-configured integration must degrade to the manual flow rather
        # than break a cabinet's day.
        for key in (
            "WHATSAPP_ENABLED",
            "WHATSAPP_TOKEN",
            "WHATSAPP_PHONE_NUMBER_ID",
            "WHATSAPP_VERIFY_TOKEN",
        ):
            monkeypatch.delenv(key, raising=False)
        assert load_config().usable is False

    def test_needs_every_piece_to_be_usable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WHATSAPP_ENABLED", "true")
        monkeypatch.setenv("WHATSAPP_TOKEN", "tok")
        monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
        assert load_config().usable is False

        monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123")
        assert load_config().usable is True


class TestWaId:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("+212 661 23 45 67", "212661234567"),
            ("0661234567", "212661234567"),
            ("00212661234567", "212661234567"),
            ("212661234567", "212661234567"),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        # A raw stored number is never a valid wa_id, and sending to one
        # silently reaches nobody.
        assert to_wa_id(raw) == expected

    def test_returns_none_for_junk(self) -> None:
        assert to_wa_id("") is None
        assert to_wa_id("n/a") is None


class TestPayload:
    def test_builds_a_template_message(self) -> None:
        payload = build_confirmation_payload(
            phone="0661234567",
            patient_name="Amina",
            cabinet_name="Cabinet Maârif",
            when=WHEN,
        )
        assert payload["to"] == "212661234567"
        assert payload["type"] == "template"
        assert payload["template"]["name"] == "appointment_confirm_fr"

    def test_parameters_are_in_template_order(self) -> None:
        # Positional in Meta's model: a wrong order renders the fields
        # transposed in the patient's message.
        payload = build_confirmation_payload(
            phone="0661234567",
            patient_name="Amina",
            cabinet_name="Cabinet Maârif",
            when=WHEN,
        )
        values = [p["text"] for p in payload["template"]["components"][0]["parameters"]]
        assert values == ["Amina", "28/07 à 15:30", "Cabinet Maârif"]

    def test_never_carries_the_specialty(self) -> None:
        payload = build_confirmation_payload(
            phone="0661234567",
            patient_name="Amina",
            cabinet_name="Cabinet Maârif",
            when=WHEN,
        )
        assert "psychiatre" not in str(payload).lower()

    def test_returns_none_for_an_unusable_number(self) -> None:
        assert (
            build_confirmation_payload(phone="n/a", patient_name="A", cabinet_name="C", when=WHEN)
            is None
        )


class TestInterpretReply:
    @pytest.mark.parametrize(
        "text", ["oui", "OUI", "Ok", "wakha", "واخا", "نعم", "je confirme", "1"]
    )
    def test_reads_a_yes(self, text: str) -> None:
        assert interpret_reply(text) is True

    @pytest.mark.parametrize("text", ["non", "NON", "annule", "لا", "cancel", "2"])
    def test_reads_a_no(self, text: str) -> None:
        assert interpret_reply(text) is False

    def test_a_button_wins_over_free_text(self) -> None:
        assert interpret_reply("peut-être", button_payload="confirm") is True
        assert interpret_reply("oui", button_payload="cancel") is False

    @pytest.mark.parametrize(
        "text",
        [
            None,
            "",
            "je ne sais pas",
            # Both keywords present: guessing either way risks cancelling a
            # visit the patient intends to keep.
            "oui mais non",
            "bonjour docteur",
        ],
    )
    def test_leaves_an_unclear_reply_unresolved(self, text: str | None) -> None:
        assert interpret_reply(text) is None

    def test_tolerates_trailing_punctuation(self) -> None:
        assert interpret_reply("Oui.") is True
        assert interpret_reply("non !") is False


class TestParseInbound:
    def _envelope(self, message: dict) -> dict:
        return {
            "entry": [
                {"changes": [{"value": {"messages": [message]}}]},
            ]
        }

    def test_extracts_a_text_message(self) -> None:
        parsed = parse_inbound(
            self._envelope(
                {
                    "from": "212661234567",
                    "id": "wamid.X",
                    "type": "text",
                    "text": {"body": "oui"},
                }
            )
        )
        assert parsed == [
            {
                "from": "212661234567",
                "text": "oui",
                "button": None,
                "message_id": "wamid.X",
            }
        ]

    def test_extracts_a_quick_reply_button(self) -> None:
        parsed = parse_inbound(
            self._envelope(
                {
                    "from": "212661234567",
                    "id": "wamid.Y",
                    "type": "button",
                    "button": {"payload": "confirm", "text": "Confirmer"},
                }
            )
        )
        assert parsed[0]["button"] == "confirm"

    def test_extracts_an_interactive_reply(self) -> None:
        parsed = parse_inbound(
            self._envelope(
                {
                    "from": "212661234567",
                    "id": "wamid.Z",
                    "type": "interactive",
                    "interactive": {"button_reply": {"id": "cancel", "title": "Annuler"}},
                }
            )
        )
        assert parsed[0]["button"] == "cancel"

    def test_handles_batched_messages(self) -> None:
        # Meta batches; taking messages[0] would silently drop replies.
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "1",
                                        "id": "a",
                                        "type": "text",
                                        "text": {"body": "oui"},
                                    },
                                    {
                                        "from": "2",
                                        "id": "b",
                                        "type": "text",
                                        "text": {"body": "non"},
                                    },
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        assert len(parse_inbound(payload)) == 2

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"entry": None},
            {"entry": [{"changes": None}]},
            {"entry": [{"changes": [{"value": {}}]}]},
            {"entry": "nonsense"},
        ],
    )
    def test_malformed_payloads_yield_nothing(self, payload: dict) -> None:
        # Raising here would 500 the webhook; Meta retries and then disables it.
        assert parse_inbound(payload) == []

    def test_ignores_delivery_status_callbacks(self) -> None:
        # Status updates carry no `messages` key at all.
        payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x"}]}}]}]}
        assert parse_inbound(payload) == []
