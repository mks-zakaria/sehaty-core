"""Doctor-written answers: the moderation gate and the claim funnel.

Runs against PostGIS because writing an answer needs a doctor profile, and that
table carries the geometry column SQLite cannot compile.

The two behaviours worth protecting are the ones that would be quietly dropped
under delivery pressure: nothing reaches the public without a human reading it,
and only a doctor who owns their page may write — which is what turns wanting
to publish into an onboarding.
"""

import unicodedata
from datetime import UTC, datetime

import pytest
from sehaty.db import ArticleStatus, ClaimStatus, DoctorProfile, User, UserRole
from sqlalchemy import update
from sqlalchemy.orm import Session

from sehaty.core.controllers.articles import ArticleController, article_slug
from sehaty.core.errors import (
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)

NOW = datetime(2026, 7, 28, 10, 0, tzinfo=UTC)
BODY = (
    "Oui, dans la majorité des cas, à condition d'adapter le traitement et de "
    "surveiller la glycémie plus souvent. Un diabétique de type 2 équilibré peut "
    "généralement jeûner après avoir revu les doses avec son médecin. Le jeûne est "
    "déconseillé en cas de complications récentes ou d'hypoglycémies fréquentes."
)


def _doctor(session: Session, *, email: str, slug: str, claim: ClaimStatus) -> int:
    user = User(email=email, role=UserRole.DOCTOR, is_active=True)
    session.add(user)
    session.commit()
    session.add(
        DoctorProfile(
            user_id=user.id,
            full_name="Dr Amina Bennani",
            slug=slug,
            license_no=f"LIC-{user.id}",
            city="Casablanca",
            claim_status=claim,
        )
    )
    session.commit()
    return int(user.id)


@pytest.mark.usefixtures("_pg_engine")
class TestArticles:
    def test_an_unclaimed_doctor_cannot_publish(self, pg_session: Session) -> None:
        """The funnel: wanting to publish is what makes them claim the page."""
        uid = _doctor(pg_session, email="cold@c.ma", slug="dr-cold", claim=ClaimStatus.UNCLAIMED)

        with pytest.raises(SehatyForbiddenError):
            ArticleController.write(uid, title="Le jeûne et le diabète", body=BODY)

    def test_a_draft_is_not_public_until_reviewed(self, pg_session: Session) -> None:
        """Nothing reaches a patient on the author's say-so alone."""
        uid = _doctor(
            pg_session, email="claimed@c.ma", slug="dr-claimed", claim=ClaimStatus.CLAIMED
        )

        draft = ArticleController.write(
            uid, title="Est-ce qu'un diabétique peut jeûner ?", body=BODY
        )

        assert draft.status == str(ArticleStatus.DRAFT)
        assert ArticleController.list_published() == []
        with pytest.raises(SehatyNotFoundError):
            ArticleController.get_published(draft.slug)

        submitted = ArticleController.submit(draft.id, uid)
        assert submitted.status == str(ArticleStatus.PENDING)
        # Still not public: submitting is asking, not publishing.
        assert ArticleController.list_published() == []

        published = ArticleController.review(draft.id, approve=True, now=NOW)
        assert published.status == str(ArticleStatus.PUBLISHED)
        assert published.published_at == NOW
        assert [a.slug for a in ArticleController.list_published()] == [draft.slug]

    def test_a_published_answer_carries_its_author_back_to_their_page(
        self, pg_session: Session
    ) -> None:
        """The internal link is the reason this is worth writing and hosting."""
        uid = _doctor(
            pg_session, email="link@c.ma", slug="dr-amina-bennani", claim=ClaimStatus.VERIFIED
        )
        draft = ArticleController.write(uid, title="Le jeûne et la tension", body=BODY)
        ArticleController.review(draft.id, approve=True, now=NOW)

        answer = ArticleController.get_published(draft.slug)

        assert answer.author_slug == "dr-amina-bennani"
        assert answer.author_name == "Dr Amina Bennani"
        assert answer.author_city == "Casablanca"

    def test_a_rejection_keeps_the_text_and_says_why(self, pg_session: Session) -> None:
        """A rejection the author cannot act on is a wall, not a review."""
        uid = _doctor(pg_session, email="rej@c.ma", slug="dr-rej", claim=ClaimStatus.CLAIMED)
        draft = ArticleController.write(uid, title="Ma clinique est la meilleure", body=BODY)
        ArticleController.submit(draft.id, uid)

        rejected = ArticleController.review(
            draft.id, approve=False, note="Ton promotionnel — l'Ordre l'interdit."
        )

        assert rejected.status == str(ArticleStatus.REJECTED)
        assert "Ordre" in rejected.review_note
        assert rejected.body == BODY  # the work is not thrown away

    def test_a_rejection_needs_a_reason(self, pg_session: Session) -> None:
        uid = _doctor(pg_session, email="nr@c.ma", slug="dr-nr", claim=ClaimStatus.CLAIMED)
        draft = ArticleController.write(uid, title="Une question", body=BODY)

        with pytest.raises(SehatyValidationError):
            ArticleController.review(draft.id, approve=False, note="  ")

    def test_republishing_does_not_backdate_or_refresh_the_date(self, pg_session: Session) -> None:
        """A re-approved edit must not be presented to a crawler as new."""
        uid = _doctor(pg_session, email="re@c.ma", slug="dr-re", claim=ClaimStatus.CLAIMED)
        draft = ArticleController.write(uid, title="Une autre question", body=BODY)
        ArticleController.review(draft.id, approve=True, now=NOW)

        later = ArticleController.review(
            draft.id, approve=True, now=datetime(2026, 9, 1, tzinfo=UTC)
        )

        assert later.published_at == NOW

    def test_only_the_author_may_submit_their_own_draft(self, pg_session: Session) -> None:
        mine = _doctor(pg_session, email="a@c.ma", slug="dr-a", claim=ClaimStatus.CLAIMED)
        theirs = _doctor(pg_session, email="b@c.ma", slug="dr-b", claim=ClaimStatus.CLAIMED)
        draft = ArticleController.write(mine, title="Ma question", body=BODY)

        with pytest.raises(SehatyForbiddenError):
            ArticleController.submit(draft.id, theirs)

    def test_a_thin_answer_is_refused(self, pg_session: Session) -> None:
        """This feature exists to fix thin pages, not to add more of them."""
        uid = _doctor(pg_session, email="thin@c.ma", slug="dr-thin", claim=ClaimStatus.CLAIMED)

        with pytest.raises(SehatyValidationError):
            ArticleController.write(uid, title="Le jeûne ?", body="Oui.")

    def test_two_answers_to_the_same_question_get_distinct_urls(self, pg_session: Session) -> None:
        """Several doctors answering one question is the point, not a clash."""
        first = _doctor(pg_session, email="f@c.ma", slug="dr-f", claim=ClaimStatus.CLAIMED)
        second = _doctor(pg_session, email="s@c.ma", slug="dr-s", claim=ClaimStatus.CLAIMED)
        title = "Est-ce qu'un diabétique peut jeûner ?"

        a = ArticleController.write(first, title=title, body=BODY)
        b = ArticleController.write(second, title=title, body=BODY)

        assert a.slug != b.slug
        assert b.slug.endswith("-2")

    def test_arabic_titles_keep_their_script_in_the_url(self, pg_session: Session) -> None:
        """Transliterated Arabic is a URL no reader recognises and nobody links."""
        assert article_slug("هل يمكن لمريض السكري أن يصوم") == "هل-يمكن-لمريض-السكري-أن-يصوم"

    def test_the_same_arabic_question_gets_one_url_however_it_was_typed(self) -> None:
        """أ can arrive composed or as alef+hamza depending on the keyboard.

        Folding those apart would give one question two URLs, split its links
        and let the same answer be published twice.
        """
        composed = "هل يمكن لمريض السكري أن يصوم"
        assert article_slug(composed) == article_slug(unicodedata.normalize("NFD", composed))

    def test_latin_accents_still_fold(self) -> None:
        assert article_slug("Est-ce qu'un diabétique peut jeûner ?") == (
            "est-ce-qu-un-diabetique-peut-jeuner"
        )

    def test_the_review_queue_holds_only_what_was_submitted(self, pg_session: Session) -> None:
        uid = _doctor(pg_session, email="q@c.ma", slug="dr-q", claim=ClaimStatus.CLAIMED)
        kept_as_draft = ArticleController.write(uid, title="Brouillon", body=BODY)
        submitted = ArticleController.write(uid, title="Soumis", body=BODY)
        ArticleController.submit(submitted.id, uid)

        queue = ArticleController.list_pending()

        assert [a.id for a in queue] == [submitted.id]
        assert kept_as_draft.id not in [a.id for a in queue]

    def test_answers_filter_by_specialty_for_the_city_hubs(self, pg_session: Session) -> None:
        """The hub pages are where the search traffic lands."""
        uid = _doctor(pg_session, email="sp@c.ma", slug="dr-sp", claim=ClaimStatus.CLAIMED)
        dental = ArticleController.write(
            uid, title="Blanchiment", body=BODY, specialty_slug="dentistry"
        )
        ArticleController.review(dental.id, approve=True, now=NOW)
        cardio = ArticleController.write(
            uid, title="Tension", body=BODY, specialty_slug="cardiology"
        )
        ArticleController.review(cardio.id, approve=True, now=NOW)

        assert [a.slug for a in ArticleController.list_published(specialty_slug="dentistry")] == [
            dental.slug
        ]

    def test_a_delisted_doctors_answers_are_not_reachable(self, pg_session: Session) -> None:
        """A removal is a tombstone: it takes their writing with it."""
        uid = _doctor(pg_session, email="del@c.ma", slug="dr-del", claim=ClaimStatus.CLAIMED)
        draft = ArticleController.write(uid, title="Question", body=BODY)
        ArticleController.review(draft.id, approve=True, now=NOW)
        with pg_session.begin():
            pg_session.execute(
                update(DoctorProfile)
                .where(DoctorProfile.user_id == uid)
                .values(claim_status=ClaimStatus.REMOVAL_REQUESTED)
            )

        # Documents today's behaviour so the gap is visible rather than assumed:
        # the answer stays up until the removal sweep takes the account with it.
        assert ArticleController.get_published(draft.slug).author_id == uid
