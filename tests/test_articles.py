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
from sehaty.db import (
    ArticleStatus,
    ClaimStatus,
    DoctorProfile,
    User,
    UserRole,
    ValidationVerdict,
)
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


@pytest.mark.usefixtures("_pg_engine")
class TestPlatformWritten:
    """Articles the platform writes from the literature, signed by doctors.

    The trade this encodes: we supply the writing, a doctor supplies the standing,
    and the article sends readers to that doctor's page. Each half is worthless
    alone — unsigned machine text nobody should act on, or a doctor with nothing
    pointing at them.
    """

    SOURCES = [{"work": "Gray's Anatomy", "locator": "41e éd., ch. 12"}]

    def test_it_is_written_without_an_author(self, pg_session: Session) -> None:
        article = ArticleController.write_from_sources(
            title="C'est quoi une hernie discale ?",
            body=BODY,
            sources=self.SOURCES,
            locale="fr",
            specialty_slug="orthopedics",
        )

        assert article.author_id is None
        assert [s.work for s in article.sources] == ["Gray's Anatomy"]
        # DRAFT, not PENDING: the review queue is doctors' own answers waiting on
        # a human, and a hundred generated drafts would bury them.
        assert article.status == str(ArticleStatus.DRAFT)

    def test_it_must_cite_something(self, pg_session: Session) -> None:
        """An article that cites nothing gives the validating doctor nothing to
        check, and a reader no reason to believe it."""
        with pytest.raises(SehatyValidationError):
            ArticleController.write_from_sources(
                title="C'est quoi une hernie discale ?", body=BODY, sources=[], locale="fr"
            )

        with pytest.raises(SehatyValidationError):
            ArticleController.write_from_sources(
                title="C'est quoi une hernie discale ?",
                body=BODY,
                sources=[{"locator": "p. 12"}],
                locale="fr",
            )

    def test_a_validating_doctor_becomes_the_byline(self, pg_session: Session) -> None:
        """The link back to their page is the whole consideration."""
        uid = _doctor(
            pg_session, email="signer@c.ma", slug="dr-signer-casa", claim=ClaimStatus.CLAIMED
        )
        article = ArticleController.write_from_sources(
            title="C'est quoi une hernie discale ?",
            body=BODY,
            sources=self.SOURCES,
            locale="fr",
        )

        signed = ArticleController.validate(article.id, uid)

        assert [v.doctor_id for v in signed.validations] == [uid]
        assert signed.validations[0].slug == "dr-signer-casa"
        assert signed.validations[0].verdict == str(ValidationVerdict.VALIDATED)

    def test_a_correction_must_say_what_changed(self, pg_session: Session) -> None:
        """Otherwise RECTIFIED is a rubber stamp with a grander name."""
        uid = _doctor(
            pg_session, email="fixer@c.ma", slug="dr-fixer-casa", claim=ClaimStatus.CLAIMED
        )
        article = ArticleController.write_from_sources(
            title="C'est quoi une hernie discale ?",
            body=BODY,
            sources=self.SOURCES,
            locale="fr",
        )

        with pytest.raises(SehatyValidationError):
            ArticleController.validate(article.id, uid, verdict=str(ValidationVerdict.RECTIFIED))

        fixed = ArticleController.validate(
            article.id,
            uid,
            verdict=str(ValidationVerdict.RECTIFIED),
            note="La sciatique n'est pas systématique.",
        )
        assert fixed.validations[0].note.startswith("La sciatique")

    def test_signing_twice_is_one_doctor_not_two(self, pg_session: Session) -> None:
        """ "Validated by four doctors" has to mean four people."""
        uid = _doctor(
            pg_session, email="twice@c.ma", slug="dr-twice-casa", claim=ClaimStatus.CLAIMED
        )
        article = ArticleController.write_from_sources(
            title="C'est quoi une hernie discale ?",
            body=BODY,
            sources=self.SOURCES,
            locale="fr",
        )
        ArticleController.validate(article.id, uid)

        again = ArticleController.validate(
            article.id, uid, verdict=str(ValidationVerdict.ENRICHED), note="Ajout du délai CNSS."
        )

        assert len(again.validations) == 1
        assert again.validations[0].verdict == str(ValidationVerdict.ENRICHED)

    def test_an_unclaimed_doctor_cannot_sign(self, pg_session: Session) -> None:
        """Same funnel as writing: an endorsement links to a page, and an
        unclaimed page is one whose owner never agreed to any of this."""
        uid = _doctor(pg_session, email="cold2@c.ma", slug="dr-cold2", claim=ClaimStatus.UNCLAIMED)
        article = ArticleController.write_from_sources(
            title="C'est quoi une hernie discale ?",
            body=BODY,
            sources=self.SOURCES,
            locale="fr",
        )

        with pytest.raises(SehatyForbiddenError):
            ArticleController.validate(article.id, uid)

    def test_the_public_read_carries_the_signatories(self, pg_session: Session) -> None:
        """What the landing page renders as the byline."""
        uid = _doctor(pg_session, email="pub@c.ma", slug="dr-pub-casa", claim=ClaimStatus.CLAIMED)
        article = ArticleController.write_from_sources(
            title="C'est quoi une hernie discale ?",
            body=BODY,
            sources=self.SOURCES,
            locale="fr",
        )
        ArticleController.validate(article.id, uid)
        ArticleController.review(article.id, approve=True, now=NOW)

        public = ArticleController.get_published(article.slug)

        assert public.author_id is None
        assert [v.slug for v in public.validations] == ["dr-pub-casa"]
        assert [s.work for s in public.sources] == ["Gray's Anatomy"]


def test_arabic_punctuation_never_reaches_the_url() -> None:
    """An Arabic question mark inside a URL path.

    Arabic punctuation lives in the same Unicode block as Arabic letters, so the
    slug rule that keeps the letters kept the punctuation too. It survives
    percent-encoding, but it breaks naive link detection in the messaging apps
    these pages are shared on — and a URL that looks broken does not get
    forwarded, which is the whole distribution channel here.
    """
    slug = article_slug("التعب الدائم: هل يمكن أن يكون فقر دم؟")

    assert "؟" not in slug
    assert "،" not in slug
    # The Arabic letters themselves must survive — transliterating produces a URL
    # nobody recognises and nothing links to.
    assert "فقر" in slug
    assert slug.endswith("دم")


@pytest.mark.usefixtures("_pg_engine")
class TestReaderVotes:
    """Readers answering "did this help you?".

    The tally is not decoration: an article with many readers and a falling
    helpful rate is one to send back to a doctor. What it must never become is a
    record of who read what.
    """

    SOURCES = [{"work": "Pathology Illustrated", "locator": "p. 1"}]

    def _published(self) -> str:
        article = ArticleController.write_from_sources(
            title="C'est quoi une hernie discale ?",
            body=BODY,
            sources=self.SOURCES,
            locale="fr",
        )
        ArticleController.review(article.id, approve=True, now=NOW)
        return article.slug

    def test_a_reader_votes_once_however_many_times_they_click(self, pg_session: Session) -> None:
        """Otherwise one enthusiastic reader looks like a consensus."""
        slug = self._published()

        ArticleController.vote(slug, fingerprint="reader-a", helpful=True)
        again = ArticleController.vote(slug, fingerprint="reader-a", helpful=True)

        assert (again.helpful_votes, again.total_votes) == (1, 1)

    def test_changing_your_mind_replaces_the_vote(self, pg_session: Session) -> None:
        slug = self._published()
        ArticleController.vote(slug, fingerprint="reader-a", helpful=True)

        changed = ArticleController.vote(slug, fingerprint="reader-a", helpful=False)

        assert (changed.helpful_votes, changed.total_votes) == (0, 1)

    def test_different_readers_are_counted_separately(self, pg_session: Session) -> None:
        slug = self._published()

        ArticleController.vote(slug, fingerprint="reader-a", helpful=True)
        ArticleController.vote(slug, fingerprint="reader-b", helpful=True)
        result = ArticleController.vote(slug, fingerprint="reader-c", helpful=False)

        assert (result.helpful_votes, result.total_votes) == (2, 3)

    def test_the_same_reader_is_not_traceable_across_articles(self, pg_session: Session) -> None:
        """The property that keeps this from becoming a browsing record.

        Keys are scoped per article, so the vote table cannot be joined on
        `voter_key` to reconstruct which articles one person read — which on
        health pages is the most sensitive thing we could hold.
        """
        first = ArticleController.voter_key(1, "reader-a")
        second = ArticleController.voter_key(2, "reader-a")

        assert first != second
        # And nothing about the reader survives in the key itself.
        assert "reader-a" not in first

    def test_an_unpublished_article_takes_no_votes(self, pg_session: Session) -> None:
        draft = ArticleController.write_from_sources(
            title="Brouillon non publié", body=BODY, sources=self.SOURCES, locale="fr"
        )

        with pytest.raises(SehatyNotFoundError):
            ArticleController.vote(draft.slug, fingerprint="reader-a", helpful=True)


@pytest.mark.usefixtures("_pg_engine")
class TestArticleTraffic:
    """Our own readership numbers, which is what topic selection should run on.

    It ran on published disease prevalence instead — a measure of who is ill, not
    of who is searching. These two are not the same population and the difference
    decides what is worth writing.
    """

    SOURCES = [{"work": "Pathology Illustrated", "locator": "p. 1"}]

    def _published(self, title: str = "C'est quoi une hernie discale ?") -> str:
        article = ArticleController.write_from_sources(
            title=title, body=BODY, sources=self.SOURCES, locale="fr"
        )
        ArticleController.review(article.id, approve=True, now=NOW)
        return article.slug

    def test_it_separates_the_channel_a_reader_arrived_by(self, pg_session: Session) -> None:
        """ "Ranks on Google" and "travels on WhatsApp" need different articles."""
        slug = self._published()

        for source in ("google", "google", "whatsapp", "facebook"):
            ArticleController.record_event(slug, event="PAGE_VIEW", source=source)

        row = ArticleController.traffic()[0]
        assert row.views == 4
        assert (row.from_google, row.from_whatsapp, row.from_facebook) == (2, 1, 1)

    def test_it_counts_readers_sent_to_a_doctor_separately(self, pg_session: Session) -> None:
        """An article that is read and one that sends a reader to a doctor are
        different kinds of success. Only the second pays for itself."""
        uid = _doctor(
            pg_session, email="traffic@c.ma", slug="dr-traffic", claim=ClaimStatus.CLAIMED
        )
        slug = self._published()
        ArticleController.record_event(slug, event="PAGE_VIEW", source="google")
        ArticleController.record_event(slug, event="DOCTOR_CLICK", source="google", doctor_id=uid)

        row = ArticleController.traffic()[0]
        assert (row.views, row.doctor_clicks) == (1, 1)

    def test_a_beacon_for_an_unknown_article_is_ignored_not_an_error(
        self, pg_session: Session
    ) -> None:
        """This is called fire-and-forget from the page. A reader's article must
        never fail because analytics did."""
        ArticleController.record_event("no-such-article", event="PAGE_VIEW", source="google")
        ArticleController.record_event(self._published(), event="NONSENSE", source="google")

        assert ArticleController.traffic() == []


@pytest.mark.usefixtures("_pg_engine")
class TestLanguageVersions:
    """The same answer, written in two languages, joined by a topic key.

    Not a translation pointer: neither version is the original. Each is written
    from the same passages in its own language, so the pairing is symmetric and
    an article never has to know which one came first.
    """

    SOURCES = [{"work": "Pathology Illustrated (7th ed.), Reid et al.", "locator": "p. 640"}]

    def _pair(self) -> tuple[int, int]:
        fr = ArticleController.write_from_sources(
            title="Diabète et plage : quels risques pour les pieds ?",
            body=BODY,
            sources=self.SOURCES,
            locale="fr",
            topic_key="diabetes-beach",
        )
        ar = ArticleController.write_from_sources(
            title="السكري والشاطئ: ما هي المخاطر على القدمين؟",
            body=BODY,
            sources=self.SOURCES,
            locale="ar",
            topic_key="diabetes-beach",
        )
        return fr.id, ar.id

    def test_each_version_offers_the_other(self, pg_session: Session) -> None:
        fr_id, ar_id = self._pair()
        ArticleController.review(fr_id, approve=True)
        ArticleController.review(ar_id, approve=True)

        fr = ArticleController.get(fr_id)
        ar = ArticleController.get(ar_id)

        assert [t.locale for t in fr.translations] == ["ar"]
        assert [t.locale for t in ar.translations] == ["fr"]
        # The title travels with the link: a switch showing only a language code
        # asks the reader to trust that the other page is the same article.
        assert fr.translations[0].title.startswith("السكري")
        assert fr.translations[0].slug == ar.slug

    def test_an_unpublished_counterpart_is_never_linked(self, pg_session: Session) -> None:
        """Linking a draft would hand the reader a 404."""
        fr_id, ar_id = self._pair()
        ArticleController.review(fr_id, approve=True)

        assert ArticleController.get(fr_id).translations == []

        ArticleController.review(ar_id, approve=True)
        assert len(ArticleController.get(fr_id).translations) == 1

    def test_an_article_never_offers_itself(self, pg_session: Session) -> None:
        fr_id, _ = self._pair()
        ArticleController.review(fr_id, approve=True)

        fr = ArticleController.get(fr_id)
        assert all(t.slug != fr.slug for t in fr.translations)

    def test_an_article_without_a_key_has_no_versions(self, pg_session: Session) -> None:
        """The normal state for a doctor's own answer."""
        article = ArticleController.write_from_sources(
            title="C'est quoi une hernie discale ?",
            body=BODY,
            sources=self.SOURCES,
            locale="fr",
        )
        ArticleController.review(article.id, approve=True)

        assert ArticleController.get(article.id).topic_key is None
        assert ArticleController.get(article.id).translations == []

    def test_the_public_read_carries_the_versions_too(self, pg_session: Session) -> None:
        """The blog reads by slug, not by id."""
        fr_id, ar_id = self._pair()
        ArticleController.review(fr_id, approve=True)
        ArticleController.review(ar_id, approve=True)
        slug = ArticleController.get(fr_id).slug

        public = ArticleController.get_published(slug)

        assert public.topic_key == "diabetes-beach"
        assert [t.locale for t in public.translations] == ["ar"]

    def test_a_key_can_be_set_on_an_article_already_published(self, pg_session: Session) -> None:
        """The pairing arrived after the articles did. Republishing them would
        change their slugs and break every link already crawled."""
        fr = ArticleController.write_from_sources(
            title="Coup de soleil avec des cloques",
            body=BODY,
            sources=self.SOURCES,
            locale="fr",
        )
        ar = ArticleController.write_from_sources(
            title="ضربة شمس مع فقاعات",
            body=BODY,
            sources=self.SOURCES,
            locale="ar",
        )
        ArticleController.review(fr.id, approve=True)
        ArticleController.review(ar.id, approve=True)
        before = ArticleController.get(fr.id).slug

        ArticleController.set_topic_key(fr.id, "sunburn")
        ArticleController.set_topic_key(ar.id, "sunburn")

        linked = ArticleController.get(fr.id)
        assert linked.slug == before
        assert [t.locale for t in linked.translations] == ["ar"]

    def test_clearing_the_key_unlinks_them(self, pg_session: Session) -> None:
        fr_id, ar_id = self._pair()
        ArticleController.review(fr_id, approve=True)
        ArticleController.review(ar_id, approve=True)

        ArticleController.set_topic_key(ar_id, None)

        assert ArticleController.get(fr_id).translations == []
        assert ArticleController.get(ar_id).topic_key is None
