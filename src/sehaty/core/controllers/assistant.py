"""The assistant: routing a patient to a doctor, and drafting translations.

Two features, one model, and a boundary between them that matters.

**For a patient.** They describe a problem in their own words — Darija, Arabic
or French — and get a specialty and the nearest doctors who practise it. The
model only chooses the specialty. The doctors come from the same geographic SQL
the search page uses, over real profiles, so the worst a bad completion can do
is send someone to a dermatologist for a rash they should have shown a GP. It
cannot invent a doctor, a phone number or an address.

That split is the whole design. A hallucinated cardiologist is a bug; a
hallucinated diagnosis is a harm. So this never diagnoses, never names a
condition, never suggests a treatment, and refuses to be talked into it.

Urgent wording does not reach the model at all. Chest pain and bleeding are
matched before any request goes out and answered with emergency numbers, because
a model call can be slow, can fail, and can be too hedged to convey urgency —
and the one case where those failures are unacceptable is the one where someone
is describing an emergency.

**For an operator.** A presentation written in one language is drafted into the
other two. Drafted, not published: the operator reads it before it is saved, and
a doctor's public page is not a place to discover that a machine wrote something
odd in a language you do not read.
"""

from __future__ import annotations

import re

from sehaty.core._dto import DomainModel
from sehaty.core.controllers.onboarding import LOCALES
from sehaty.core.services import llm

# Specialties the assistant may route to. Constrained to what the directory
# actually has: routing someone to a rheumatologist we cannot show them is a
# worse answer than routing them to a generalist we can.
ROUTABLE = {
    "generalist": "Médecin généraliste",
    "dentistry": "Dentiste",
    "cardiology": "Cardiologue",
    "dermatology": "Dermatologue",
    "gynecology": "Gynécologue",
    "ophthalmology": "Ophtalmologue",
    "pediatrics": "Pédiatre",
    "psychiatry": "Psychiatre",
    "otolaryngology": "ORL",
    "orthopedics": "Orthopédiste",
}

# Matched before the model is called. Deliberately broad — a false alarm costs
# someone a phone call, a miss costs more — and covering the three languages a
# patient actually types in.
URGENT = re.compile(
    r"douleur\s+(à|a)\s+la\s+poitrine|mal\s+de\s+poitrine|crise\s+cardiaque|avc"
    r"|h[ée]morragie|saigne\w*\s+beaucoup|inconscient"
    # "perte de connaissance" and "a perdu connaissance" are the same event and
    # both get typed; matching only the noun missed the commoner phrasing.
    r"|(perte|perd\w*)\s+(de\s+)?connaissance|[ée]vanoui\w*|convulsi\w*"
    r"|suicid\w*|respire\s+plus|[ée]touffe"
    r"|ألم\s+في\s+الصدر|نزيف|فقدان\s+الوعي|انتحار|ما\s*كيتنفسش|كيتنفسش"
    r"|قلبي|الصدر\s+كيوجعني",
    re.IGNORECASE,
)

EMERGENCY_NUMBERS = "150 (SAMU) · 190 (police) · 15 (pompiers)"

_ROUTABLE_LIST = ", ".join(ROUTABLE)

_TRIAGE_SYSTEM = f"""You route a patient to the right kind of doctor in Morocco.

You will be given a complaint in French, Arabic or Moroccan Darija.

Return JSON only, of the shape:
  specialty: one slug, reason: one short sentence, confidence: a number 0..1

Rules you must not break:
- `specialty` must be exactly one of: {_ROUTABLE_LIST}
- When unsure, choose "generalist". A generalist is always a safe answer.
- NEVER name a disease, a diagnosis, a medicine or a treatment.
- `reason` explains only which kind of doctor and why, in one sentence, in the
  same language the patient used.
- You are not a doctor and must not imply that you are.
"""

_TRANSLATE_SYSTEM = """You translate a Moroccan doctor's public presentation.

Return JSON only: {"fr": "...", "ar": "...", "ary": "..."}

- `fr` is French, `ar` is Modern Standard Arabic, `ary` is Moroccan Darija in
  Arabic script — Darija as spoken in Casablanca, not a transliteration of MSA.
- Keep the meaning and the length. Do not add qualifications, claims, prices,
  or praise the original does not contain: the Ordre restricts promotional
  language and it is the doctor who answers for it.
- Keep proper nouns, street names and specialty terms as they are.
- Return the source language unchanged.
"""


class Triage(DomainModel):
    """Where the assistant thinks this patient should go."""

    specialty_slug: str
    specialty_label: str
    reason: str
    confidence: float
    # True when the wording looked urgent and the model was never consulted.
    is_emergency: bool = False
    emergency_numbers: str | None = None


class TranslationDraft(DomainModel):
    """A presentation in each language, for an operator to read before saving."""

    fr: str | None = None
    ar: str | None = None
    ary: str | None = None


class AssistantController:
    @staticmethod
    def available() -> bool:
        return llm.is_configured()

    @staticmethod
    def diagnose() -> tuple[bool, str]:
        """Whether the provider actually answers, and what it said if not.

        Admin-only above this layer: the detail includes the provider's own
        error text, which is exactly what an operator needs and exactly what a
        patient should never see.
        """
        return llm.diagnose()

    @staticmethod
    def triage(complaint: str, *, locale: str = "fr") -> Triage:
        """Turn a described problem into a specialty to search for.

        Falls back to `generalist` whenever the model is unavailable or answers
        with something unexpected. A directory that quietly sends everyone to a
        GP still works; one that errors because a key expired does not.
        """
        text = (complaint or "").strip()
        if len(text) < 3:
            raise ValueError("describe the problem in a few words")

        if URGENT.search(text):
            # Never reaches the model: a call can be slow, can fail, and can
            # hedge — and this is the one case where it must not.
            return Triage(
                specialty_slug="generalist",
                specialty_label=ROUTABLE["generalist"],
                reason=_emergency_line(locale),
                confidence=1.0,
                is_emergency=True,
                emergency_numbers=EMERGENCY_NUMBERS,
            )

        fallback = Triage(
            specialty_slug="generalist",
            specialty_label=ROUTABLE["generalist"],
            reason=_generalist_line(locale),
            confidence=0.0,
        )
        if not llm.is_configured():
            return fallback

        try:
            answer = llm.complete_json(
                system=_TRIAGE_SYSTEM,
                user=f"Patient language: {locale}\nComplaint: {text}",
                temperature=0.1,
                max_tokens=300,
            )
        except llm.LLMUnavailable:
            return fallback

        slug = str(answer.get("specialty", "")).strip()
        if slug not in ROUTABLE:
            # A specialty we cannot show is worse than a generalist we can.
            return fallback
        try:
            confidence = min(max(float(answer.get("confidence", 0.5)), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = 0.5

        return Triage(
            specialty_slug=slug,
            specialty_label=ROUTABLE[slug],
            reason=str(answer.get("reason", "")).strip() or _generalist_line(locale),
            confidence=confidence,
        )

    @staticmethod
    def translate(text: str, *, source_locale: str) -> TranslationDraft:
        """Draft a presentation in the other two languages.

        A draft, never a save. The operator reads it first — a doctor's public
        page is not where you want to discover a machine wrote something strange
        in a language you do not read.
        """
        text = (text or "").strip()
        if len(text) < 10:
            raise ValueError("write the presentation first")
        if source_locale not in LOCALES:
            raise ValueError(f"unsupported language {source_locale!r}")
        if not llm.is_configured():
            raise llm.LLMUnavailable("the assistant is not configured yet")

        answer = llm.complete_json(
            system=_TRANSLATE_SYSTEM,
            user=f"Source language: {source_locale}\nText:\n{text}",
            temperature=0.2,
            max_tokens=900,
        )
        draft = TranslationDraft(**{k: str(answer.get(k, "")).strip() or None for k in LOCALES})
        # Whatever the model returns for the source language, the operator's own
        # words win. They wrote them on purpose.
        setattr(draft, source_locale, text)
        return draft


def _emergency_line(locale: str) -> str:
    return {
        "fr": f"Cela peut être urgent. Appelez immédiatement le {EMERGENCY_NUMBERS}.",
        "ar": f"قد تكون حالة مستعجلة. اتصلوا فورًا بـ {EMERGENCY_NUMBERS}.",
        "ary": f"يمكن تكون حالة مستعجلة. عيّط دابا ل {EMERGENCY_NUMBERS}.",
    }.get(locale, f"Urgent — {EMERGENCY_NUMBERS}")


def _generalist_line(locale: str) -> str:
    return {
        "fr": "Commencez par un médecin généraliste, il vous orientera si besoin.",
        "ar": "ابدأوا بطبيب عام، وسيوجّهكم عند الحاجة.",
        "ary": "بدا بطبيب عام، هو اللي غادي يوجّهك إلا خاص.",
    }.get(locale, "Start with a general practitioner.")
