"""WhatsApp Cloud API: send confirmation asks, interpret replies.

This is v2 of the confirmation channel. v1 is the secretary tapping a pre-filled
`wa.me` link, which needs none of this and delivers most of the value; the
automation only pays off once enough cabinets are live that tapping links stops
scaling.

**It is gated, and off by default.** Proactive template messages require Meta
Business verification, an approved Utility-category template and a dedicated
number — weeks of lead time. Until `WHATSAPP_ENABLED` is set with a token and a
phone-number id, `send_confirmation` reports "not configured" rather than
failing: a half-configured integration must degrade to the manual flow, not
break a cabinet's day.

**Message content carries no health data.** The template names the cabinet and
the time, never the specialty. "Rendez-vous avec le Dr X, psychiatre" arriving
on a shared family phone discloses a patient's condition to whoever picks it up.

Delivery itself is done by the caller (the API layer owns HTTP). This module
builds the payload, parses inbound webhooks, and decides what a reply means.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

# Approved Utility-category template. Quick Reply buttons give the two-option
# "poll": Confirmer / Annuler.
CONFIRM_TEMPLATE = "appointment_confirm_fr"
REMINDER_TEMPLATE = "appointment_reminder_fr"

# Free-text answers patients actually send, in French, Darija and Arabic.
_YES = {
    "oui",
    "ok",
    "okay",
    "yes",
    "confirme",
    "confirmé",
    "je confirme",
    "d'accord",
    "daccord",
    "wakha",
    "wa5a",
    "واخا",
    "نعم",
    "أكيد",
    "موافق",
    "1",
}
_NO = {
    "non",
    "no",
    "annule",
    "annulé",
    "j'annule",
    "jannule",
    "cancel",
    "la",
    "lla",
    "لا",
    "ما نقدرش",
    "إلغاء",
    "2",
}


@dataclass(frozen=True)
class WhatsAppConfig:
    """Cloud API credentials, read from the environment."""

    enabled: bool
    token: str | None
    phone_number_id: str | None
    verify_token: str | None

    @property
    def usable(self) -> bool:
        """True only when every piece needed to actually send is present."""
        return bool(self.enabled and self.token and self.phone_number_id)


def load_config() -> WhatsAppConfig:
    """Read the integration config. Absent env means disabled, not broken."""
    return WhatsAppConfig(
        enabled=os.environ.get("WHATSAPP_ENABLED", "").lower() in {"1", "true", "yes"},
        token=os.environ.get("WHATSAPP_TOKEN") or None,
        phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID") or None,
        verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN") or None,
    )


def to_wa_id(phone: str) -> str | None:
    """Normalize to the bare international form the Cloud API expects.

    No `+`, no spaces, no leading zero — a raw stored number is never a valid
    `wa_id`, and sending to one silently reaches nobody.
    """
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = f"212{digits[1:]}"
    return digits or None


def build_confirmation_payload(
    *, phone: str, patient_name: str, cabinet_name: str, when: datetime
) -> dict | None:
    """The Cloud API request body for a T-24h confirmation ask.

    Returns None when the number cannot be normalized, so the caller records a
    failure instead of posting a request that can only be rejected.

    Body parameters are positional in Meta's template model: their order must
    match the approved template exactly, or the message renders with the fields
    transposed.
    """
    wa_id = to_wa_id(phone)
    if not wa_id:
        return None

    return {
        "messaging_product": "whatsapp",
        "to": wa_id,
        "type": "template",
        "template": {
            "name": CONFIRM_TEMPLATE,
            "language": {"code": "fr"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": patient_name},
                        {"type": "text", "text": when.strftime("%d/%m à %H:%M")},
                        # The cabinet, never the specialty.
                        {"type": "text", "text": cabinet_name},
                    ],
                }
            ],
        },
    }


def interpret_reply(text: str | None, button_payload: str | None = None) -> bool | None:
    """Decide what a patient's reply means.

    Returns True (coming), False (not coming) or None (unclear).

    A Quick Reply button is unambiguous and wins over free text. Unclear replies
    stay unclear on purpose: guessing that "je ne sais pas" means yes would
    leave a slot held for someone who never comes, which is the exact cost this
    system exists to remove.
    """
    if button_payload:
        payload = button_payload.strip().lower()
        if payload in {"confirm", "yes", "oui"}:
            return True
        if payload in {"cancel", "no", "non"}:
            return False

    if not text:
        return None
    normalized = text.strip().lower().rstrip(" .!،")
    if normalized in _YES:
        return True
    if normalized in _NO:
        return False

    # Only accept a keyword inside a longer sentence when the other side is not
    # also present — "oui mais non" must not resolve to yes.
    has_yes = any(token in normalized.split() for token in _YES)
    has_no = any(token in normalized.split() for token in _NO)
    if has_yes and not has_no:
        return True
    if has_no and not has_yes:
        return False
    return None


def parse_inbound(payload: dict) -> list[dict]:
    """Flatten a Cloud API webhook into ``{from, text, button, message_id}``.

    Meta nests messages four levels deep and batches them, so a naive
    ``payload["messages"][0]`` drops replies. Malformed shapes yield an empty
    list rather than raising — a webhook handler that 500s gets retried
    forever and then disabled by Meta.
    """
    out: list[dict] = []
    try:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                for message in value.get("messages") or []:
                    button = None
                    if message.get("type") == "button":
                        button = (message.get("button") or {}).get("payload")
                    elif message.get("type") == "interactive":
                        interactive = message.get("interactive") or {}
                        button = (interactive.get("button_reply") or {}).get("id")
                    out.append(
                        {
                            "from": message.get("from"),
                            "text": (message.get("text") or {}).get("body"),
                            "button": button,
                            "message_id": message.get("id"),
                        }
                    )
    except (AttributeError, TypeError):
        return []
    return out
