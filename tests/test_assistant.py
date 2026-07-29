"""The assistant's safety rules, which hold with or without a model.

Every test here runs with GROQ_API_KEY unset, because that is both today's
reality and the state the code must never break in: the directory works without
the assistant, so an unconfigured deployment degrades rather than fails.

The rules being pinned are the ones that would be quietly lost the day someone
adds a key and starts tuning prompts.
"""

import pytest

from sehaty.core.controllers.assistant import (
    EMERGENCY_NUMBERS,
    ROUTABLE,
    AssistantController,
)
from sehaty.core.services import llm


@pytest.fixture(autouse=True)
def _no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


class TestEmergencies:
    """Urgent wording must never depend on a network call succeeding."""

    @pytest.mark.parametrize(
        "complaint",
        [
            "j'ai une douleur à la poitrine depuis ce matin",
            "mon père a perdu connaissance",
            "عندي ألم في الصدر",
            "كيتنفسش مزيان",
            "je pense au suicide",
        ],
    )
    def test_urgent_wording_answers_with_emergency_numbers(self, complaint: str) -> None:
        result = AssistantController.triage(complaint, locale="fr")

        assert result.is_emergency is True
        assert result.emergency_numbers == EMERGENCY_NUMBERS
        assert "150" in result.reason or "150" in (result.emergency_numbers or "")

    def test_the_emergency_answer_is_in_the_patients_language(self) -> None:
        darija = AssistantController.triage("عندي ألم في الصدر", locale="ary")

        assert darija.is_emergency is True
        assert "عيّط" in darija.reason


class TestDegradesWithoutAKey:
    def test_it_reports_itself_unavailable(self) -> None:
        assert AssistantController.available() is False

    def test_triage_still_answers_rather_than_failing(self) -> None:
        """A directory that sends everyone to a GP works; one that 500s does not."""
        result = AssistantController.triage("j'ai mal aux dents depuis deux jours")

        assert result.specialty_slug == "generalist"
        assert result.confidence == 0.0
        assert result.reason

    def test_translation_refuses_out_loud(self) -> None:
        """Unlike triage, there is no honest fallback for a translation."""
        with pytest.raises(llm.LLMUnavailable):
            AssistantController.translate(
                "Cabinet dentaire au Maârif, soins et prothèses.", source_locale="fr"
            )


class TestGuards:
    def test_every_routable_specialty_exists_in_the_directory(self) -> None:
        """Routing to a specialty we cannot show is a worse answer than a GP."""
        from sehaty.core.controllers.landing_config import SPECIALTY_TEMPLATES

        for slug in ROUTABLE:
            assert slug in SPECIALTY_TEMPLATES, slug

    def test_an_empty_complaint_is_refused(self) -> None:
        with pytest.raises(ValueError):
            AssistantController.triage("  ")

    def test_an_unsupported_language_is_refused(self) -> None:
        with pytest.raises(ValueError):
            AssistantController.translate("Un texte assez long pour passer.", source_locale="es")
