"""Doctor-written answers: writing, reviewing, publishing, reading.

The directory's weakness is that three thousand listings differing only by name
and street are what a search engine crawls and declines to index. Answers written
by named physicians are the fix, and unlike the listings they cannot be scraped
back off us.

Publication is not the author's call. What appears under the platform's name is
the platform's liability, and an answer that reads as advertising is one the
*doctor* answers for in front of their council — so every submission is read by
a human before it is public, and a rejection carries a reason the author can act
on rather than vanishing.

Only a doctor who has claimed their page may write. That is the point as much as
a safeguard: wanting to publish is what turns an imported listing into an
onboarded one, without anyone driving across Casablanca.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import UTC, datetime, timedelta

from sehaty.db import (
    Article,
    ArticleEvent,
    ArticleEventType,
    ArticleStatus,
    ArticleValidation,
    ArticleVote,
    ClaimStatus,
    DoctorProfile,
    User,
    UserRole,
    ValidationVerdict,
)
from sqlalchemy import case, func, select

from sehaty.core import config
from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import (
    SehatyForbiddenError,
    SehatyNotFoundError,
    SehatyValidationError,
)

MIN_BODY_CHARS = 120
MAX_BODY_CHARS = 12_000
MAX_TITLE_CHARS = 300


class ImageRef(DomainModel):
    """One illustration, in whichever stage it has reached.

    `brief` says what the picture should show and arrives with the draft; `url`
    appears only once a real image has been sourced. A brief with no url renders
    nothing on the page — a fabricated medical diagram is worse than none,
    because a reader trusts a picture of an artery far more readily than a
    sentence about one and cannot check it.
    """

    brief: str | None = None
    alt: str | None = None
    url: str | None = None
    credit: str | None = None
    credit_url: str | None = None


class SourceRef(DomainModel):
    """One work a platform-written article was drawn from."""

    work: str
    # Edition, chapter, page — whatever lets a doctor find the passage again.
    locator: str | None = None


class ValidatorRef(DomainModel):
    """A doctor who put their name to an article.

    Carries the doctor's slug because the link back to their page is the whole
    consideration: they lend an article their professional standing, and the
    article sends readers to them.
    """

    doctor_id: int
    full_name: str | None
    slug: str | None
    city: str | None
    # "VALIDATED", "RECTIFIED" or "ENRICHED" — three different amounts of work,
    # and a reader is owed the difference between "agreed with" and "corrected".
    verdict: str
    note: str | None
    validated_at: datetime | None


class ArticleTraffic(DomainModel):
    """One article's readership over a period, split by channel."""

    slug: str
    title: str
    locale: str
    specialty_slug: str | None
    views: int
    # Readers who followed a validating doctor's name to their page. The number
    # that makes signing an article worth a doctor's five minutes.
    doctor_clicks: int
    from_google: int
    from_whatsapp: int
    from_facebook: int


class TranslationRef(DomainModel):
    """The same answer, published in another language.

    Title as well as slug: a language switch that shows only a language code
    asks the reader to trust that the other page is the same article. Showing
    its title lets them see that it is.
    """

    locale: str
    slug: str
    title: str


class ArticleView(DomainModel):
    """One answer, with the byline a reader needs to weigh it."""

    id: int
    slug: str
    title: str
    summary: str | None
    body: str
    locale: str
    # Groups the versions of one answer written in different languages.
    topic_key: str | None = None
    specialty_slug: str | None
    status: str
    published_at: datetime | None
    # Set on a draft that is queued to publish itself. None on everything else.
    scheduled_for: datetime | None = None
    review_note: str | None

    # None when the platform wrote it from the literature rather than a doctor
    # answering a question. Such an article stands on `sources` and on the
    # doctors in `validations` instead of on one byline.
    author_id: int | None
    author_name: str | None
    # Links the answer back to its author's page — the internal link that makes
    # this worth writing for the doctor and worth having for the directory.
    author_slug: str | None
    author_city: str | None

    sources: list[SourceRef] = []
    images: list[ImageRef] = []
    # Other published languages of this same answer. Empty unless the article
    # carries a topic key and a counterpart is published.
    translations: list[TranslationRef] = []
    validations: list[ValidatorRef] = []

    # How readers answered "did this help you?". Shown on the page, but the
    # reason they exist is editorial: an article with many readers and a falling
    # helpful rate is one to send back to a doctor. A score nobody acts on is
    # decoration.
    helpful_votes: int = 0
    total_votes: int = 0


def article_slug(title: str, *, suffix: int | None = None) -> str:
    """URL-safe slug from the question itself.

    Arabic is kept as Arabic. Transliterating "هل يمكن لمريض السكري أن يصوم" into
    latin letters produces something no reader recognises and no one links to;
    modern browsers and search engines handle the encoded original fine.
    """
    decomposed = unicodedata.normalize("NFKD", title.strip().lower())
    # Drop only the Latin combining marks, so "é" folds to "e". Arabic marks
    # live above U+0600 and are kept, then recomposed: decomposing أ into alef +
    # hamza would give the same question two different URLs depending on how the
    # author's keyboard happened to emit it.
    folded = "".join(c for c in decomposed if not (0x0300 <= ord(c) <= 0x036F))
    recomposed = unicodedata.normalize("NFKC", folded)
    # Arabic punctuation sits inside the same block as Arabic letters, so the
    # range below keeps it unless it is removed by name. It has to go: an article
    # titled "هل هو فقر دم؟" was producing a slug ending in ؟, i.e. a question
    # mark inside a URL path. It survives percent-encoding, but it breaks naive
    # link detection in the messaging apps these pages are shared on, and a URL
    # that looks broken does not get forwarded.
    recomposed = recomposed.translate({ord(c): None for c in "؟،؛۔٪٫٬“”«»"})
    slug = re.sub(r"[^\w؀-ۿ]+", "-", recomposed, flags=re.UNICODE).strip("-")
    slug = slug[:280] or "question"
    return f"{slug}-{suffix}" if suffix else slug


def _unique_slug(session, base: str) -> str:  # noqa: ANN001
    """`base`, or `base-2`/`base-3` — a published URL is never reassigned."""
    candidate, n = base, 1
    while session.execute(select(Article.id).where(Article.slug == candidate)).scalar_one_or_none():
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def _check_text(*, title: str, body: str, locale: str) -> None:
    """The rules every article obeys, however it was written.

    Shared by the doctor's own answer and the platform's, because a length floor
    that applies to one and not the other is how a corpus of generated stubs ends
    up published under the same masthead as real answers.
    """
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or len(title) > MAX_TITLE_CHARS:
        raise SehatyValidationError(f"title must be 1-{MAX_TITLE_CHARS} characters")
    if len(body) < MIN_BODY_CHARS:
        # Not gatekeeping length for its own sake: a two-line answer with no
        # reasoning is the kind of thin content this feature exists to avoid
        # adding more of.
        raise SehatyValidationError(
            f"an answer needs at least {MIN_BODY_CHARS} characters to be worth publishing"
        )
    if len(body) > MAX_BODY_CHARS:
        raise SehatyValidationError(f"at most {MAX_BODY_CHARS} characters")
    if locale not in {"ar", "ary", "fr"}:
        raise SehatyValidationError(f"unsupported locale {locale!r}")


def _sources(raw) -> list[SourceRef]:  # noqa: ANN001
    """Stored JSON -> typed refs, skipping anything without a work name.

    Tolerant on read: a malformed row must not 500 a published page. What it must
    not do is invent a citation, so entries with no `work` are dropped rather
    than rendered as an empty bullet under "Sources".
    """
    return [
        SourceRef(work=str(item["work"]).strip(), locator=(item.get("locator") or None))
        for item in (raw or [])
        if isinstance(item, dict) and str(item.get("work", "")).strip()
    ]


def _images(raw) -> list[ImageRef]:  # noqa: ANN001
    """Stored JSON -> typed refs, dropping entries that say nothing.

    Tolerant on read for the same reason as `_sources`: a malformed row must not
    500 a published page.
    """
    return [
        ImageRef(
            brief=item.get("brief") or None,
            alt=item.get("alt") or None,
            url=item.get("url") or None,
            credit=item.get("credit") or None,
            credit_url=item.get("credit_url") or None,
        )
        for item in (raw or [])
        if isinstance(item, dict) and (item.get("brief") or item.get("url"))
    ]


def _translations_for(session, article) -> list[TranslationRef]:  # noqa: ANN001
    """The same answer in other languages, published only.

    A draft counterpart must not be linked: the switch would hand a reader a
    404. Restricted to PUBLISHED for the same reason the public read is.
    """
    topic_key = getattr(article, "topic_key", None)
    if not topic_key:
        return []
    rows = session.execute(
        select(Article.locale, Article.slug, Article.title)
        .where(
            Article.topic_key == topic_key,
            Article.id != article.id,
            Article.status == ArticleStatus.PUBLISHED,
        )
        .order_by(Article.locale.asc())
    ).all()
    return [TranslationRef(locale=r.locale, slug=r.slug, title=r.title) for r in rows]


def _view(
    row,  # noqa: ANN001
    validations: list[ValidatorRef] | None = None,
    votes: tuple[int, int] = (0, 0),
    translations: list[TranslationRef] | None = None,
) -> ArticleView:
    return ArticleView(
        id=row.id,
        slug=row.slug,
        title=row.title,
        summary=row.summary,
        body=row.body,
        locale=row.locale,
        topic_key=getattr(row, "topic_key", None),
        specialty_slug=row.specialty_slug,
        status=str(row.status),
        published_at=row.published_at,
        scheduled_for=getattr(row, "scheduled_for", None),
        review_note=row.review_note,
        author_id=row.author_id,
        author_name=getattr(row, "full_name", None),
        author_slug=getattr(row, "author_slug", None),
        author_city=getattr(row, "city", None),
        sources=_sources(getattr(row, "sources", None)),
        images=_images(getattr(row, "images", None)),
        validations=validations or [],
        translations=translations or [],
        helpful_votes=votes[0],
        total_votes=votes[1],
    )


_SELECT = select(
    Article.id,
    Article.slug,
    Article.title,
    Article.summary,
    Article.body,
    Article.locale,
    Article.topic_key,
    Article.specialty_slug,
    Article.status,
    Article.published_at,
    Article.scheduled_for,
    Article.review_note,
    Article.author_id,
    Article.sources,
    Article.images,
    DoctorProfile.full_name,
    DoctorProfile.slug.label("author_slug"),
    DoctorProfile.city,
).join(DoctorProfile, DoctorProfile.user_id == Article.author_id, isouter=True)


_VALIDATION_SELECT = select(
    ArticleValidation.article_id,
    ArticleValidation.doctor_id,
    ArticleValidation.verdict,
    ArticleValidation.note,
    ArticleValidation.created_at,
    DoctorProfile.full_name,
    DoctorProfile.slug,
    DoctorProfile.city,
).join(DoctorProfile, DoctorProfile.user_id == ArticleValidation.doctor_id, isouter=True)


def _votes_for(session, article_ids: list[int]) -> dict[int, tuple[int, int]]:  # noqa: ANN001
    """(helpful, total) per article, in one query.

    Batched for the same reason as the validators: the index renders a dozen
    cards and a query per card is how a list page gets slow enough that a crawler
    gives up on it.
    """
    if not article_ids:
        return {}
    rows = session.execute(
        select(
            ArticleVote.article_id,
            func.count(ArticleVote.id),
            func.sum(case((ArticleVote.helpful.is_(True), 1), else_=0)),
        )
        .where(ArticleVote.article_id.in_(article_ids))
        .group_by(ArticleVote.article_id)
    ).all()
    return {row[0]: (int(row[2] or 0), int(row[1] or 0)) for row in rows}


def _validations_for(session, article_ids: list[int]) -> dict[int, list[ValidatorRef]]:  # noqa: ANN001
    """Every validator of every given article, in one query.

    Batched because the index page renders a dozen articles and each one shows
    its signatories; a query per card is how a list page becomes slow enough that
    a crawler gives up on it.
    """
    if not article_ids:
        return {}
    rows = session.execute(
        _VALIDATION_SELECT.where(ArticleValidation.article_id.in_(article_ids)).order_by(
            ArticleValidation.created_at.asc()
        )
    ).all()

    by_article: dict[int, list[ValidatorRef]] = {}
    for row in rows:
        by_article.setdefault(row.article_id, []).append(
            ValidatorRef(
                doctor_id=row.doctor_id,
                full_name=row.full_name,
                slug=row.slug,
                city=row.city,
                verdict=str(row.verdict),
                note=row.note,
                validated_at=row.created_at,
            )
        )
    return by_article


class ArticleController:
    @staticmethod
    def write(
        author_id: int,
        *,
        title: str,
        body: str,
        summary: str | None = None,
        locale: str = "ar",
        specialty_slug: str | None = None,
    ) -> ArticleView:
        """Create a draft. Only a doctor whose page is claimed may write."""
        _check_text(title=title, body=body, locale=locale)

        with get_session() as session:
            user = session.get(User, author_id)
            if user is None or user.role != UserRole.DOCTOR:
                raise SehatyForbiddenError("only doctors may publish answers")
            profile = session.get(DoctorProfile, author_id)
            if profile is None:
                raise SehatyNotFoundError(f"no doctor profile for user {author_id}")
            if profile.claim_status == ClaimStatus.UNCLAIMED:
                # The whole funnel: publishing requires owning your page.
                raise SehatyForbiddenError("claim your page before publishing")

            article = Article(
                author_id=author_id,
                title=title.strip(),
                slug=_unique_slug(session, article_slug(title)),
                summary=(summary or "").strip() or None,
                body=body.strip(),
                locale=locale,
                specialty_slug=specialty_slug,
                status=ArticleStatus.DRAFT,
            )
            session.add(article)
            session.flush()
            article_id = article.id

        return ArticleController.get(article_id)

    @staticmethod
    def write_from_sources(
        *,
        title: str,
        body: str,
        sources: list[dict],
        summary: str | None = None,
        locale: str = "ar",
        specialty_slug: str | None = None,
        images: list[dict] | None = None,
        topic_key: str | None = None,
        scheduled_for: datetime | None = None,
    ) -> ArticleView:
        """Create a draft the platform wrote from the literature.

        No author: this is not anyone's professional opinion yet. It becomes
        publishable when doctors sign it — which is why at least one source is
        required here and not merely encouraged. An article about a disease that
        cites nothing gives the doctor being asked to validate it no way to check
        anything, and gives a reader no reason to believe it over any other page
        on the internet.

        Starts DRAFT rather than PENDING so the review queue stays what it is —
        doctors' own answers waiting on a human — instead of filling with a
        hundred generated drafts nobody has read yet.
        """
        cited = [
            {"work": str(item["work"]).strip(), "locator": (item.get("locator") or None)}
            for item in (sources or [])
            if isinstance(item, dict) and str(item.get("work", "")).strip()
        ]
        if not cited:
            raise SehatyValidationError("a platform-written article must cite at least one source")

        _check_text(title=title, body=body, locale=locale)

        with get_session() as session:
            article = Article(
                author_id=None,
                title=title.strip(),
                slug=_unique_slug(session, article_slug(title)),
                summary=(summary or "").strip() or None,
                body=body.strip(),
                locale=locale,
                topic_key=(topic_key or "").strip() or None,
                scheduled_for=scheduled_for,
                specialty_slug=specialty_slug,
                sources=cited,
                images=[i for i in (images or []) if isinstance(i, dict)],
                status=ArticleStatus.DRAFT,
            )
            session.add(article)
            session.flush()
            article_id = article.id

        return ArticleController.get(article_id)

    @staticmethod
    def schedule(article_id: int, when: datetime | None) -> ArticleView:
        """Queue a draft to publish itself, or take it back off the queue.

        Refuses an article that is already published: the date would say it is
        waiting when it is not, and the sweep skips it anyway. Refuses a
        rejection outright — a piece turned down for being wrong must not become
        public because a clock came round.
        """
        with get_session() as session:
            article = session.get(Article, article_id)
            if article is None:
                raise SehatyNotFoundError(f"no article {article_id}")
            if when is not None and article.status == ArticleStatus.PUBLISHED:
                raise SehatyValidationError("that article is already published")
            if when is not None and article.status == ArticleStatus.REJECTED:
                raise SehatyValidationError("a rejected article cannot be scheduled")
            article.scheduled_for = when
            session.flush()
        return ArticleController.get(article_id)

    @staticmethod
    def list_scheduled(limit: int = 100) -> list[ArticleView]:
        """What is queued, soonest first — the editorial calendar."""
        with get_session() as session:
            rows = session.execute(
                _SELECT.where(
                    Article.scheduled_for.is_not(None),
                    Article.status != ArticleStatus.PUBLISHED,
                )
                .order_by(Article.scheduled_for.asc())
                .limit(limit)
            ).all()
            signed = _validations_for(session, [r.id for r in rows])
            votes = _votes_for(session, [r.id for r in rows])
        return [_view(r, signed.get(r.id), votes.get(r.id, (0, 0))) for r in rows]

    @staticmethod
    def publish_due(*, now: datetime | None = None, limit: int = 50) -> list[ArticleView]:
        """Publish every draft whose time has come. Returns what went live.

        The sweep the scheduler runs. Three things it deliberately does not do:

        It never touches a draft with no date — that draft is unfinished, not
        waiting, and the difference is the only thing standing between a
        scheduler and a machine that publishes whatever it finds.

        It never resurrects a rejection. A piece turned down for being wrong
        stays down whatever its date says.

        It clears the date as it publishes, so a replay cannot republish and
        move an article's publication date forward — which would present old
        writing to a reader, and to a crawler, as new.
        """
        now = now or datetime.now(UTC)
        with get_session() as session:
            due = (
                session.execute(
                    select(Article.id)
                    .where(
                        Article.scheduled_for.is_not(None),
                        Article.scheduled_for <= now,
                        Article.status.in_([ArticleStatus.DRAFT, ArticleStatus.PENDING]),
                    )
                    .order_by(Article.scheduled_for.asc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )

        published: list[ArticleView] = []
        for article_id in due:
            with get_session() as session:
                article = session.get(Article, article_id)
                if article is None:  # pragma: no cover - deleted mid-sweep
                    continue
                article.status = ArticleStatus.PUBLISHED
                article.published_at = article.published_at or now
                article.review_note = None
                article.scheduled_for = None
                session.flush()
            published.append(ArticleController.get(article_id))
        return published

    @staticmethod
    def set_topic_key(article_id: int, topic_key: str | None) -> ArticleView:
        """Group an already-published article with its other languages.

        Needed because the pairing was introduced after the articles were: the
        alternative is republishing them, which would change their slugs and
        break every link already crawled.
        """
        with get_session() as session:
            article = session.get(Article, article_id)
            if article is None:
                raise SehatyNotFoundError(f"no article {article_id}")
            article.topic_key = (topic_key or "").strip() or None
            session.flush()
        return ArticleController.get(article_id)

    @staticmethod
    def validate(
        article_id: int,
        doctor_id: int,
        *,
        verdict: str = str(ValidationVerdict.VALIDATED),
        note: str | None = None,
    ) -> ArticleView:
        """Record that a doctor read this article and stands behind it.

        Only a doctor who has claimed their page, for the same reason writing
        requires it: an endorsement links to a doctor's page, and a page nobody
        has claimed is one whose owner never agreed to any of this.

        A correction or an addition must say what it was. A `RECTIFIED` with no
        note is indistinguishable from a rubber stamp, and the note is what makes
        the credit legible to the next reader — and to the next doctor deciding
        whether signing these is worth five minutes.

        Idempotent per doctor: reading it again updates their verdict instead of
        stacking a second endorsement, so "validated by four doctors" always
        means four people.
        """
        try:
            chosen = ValidationVerdict(verdict)
        except ValueError:
            raise SehatyValidationError(f"unknown verdict {verdict!r}") from None

        note = (note or "").strip() or None
        if chosen is not ValidationVerdict.VALIDATED and not note:
            raise SehatyValidationError(f"say what you changed: a {chosen} needs a note")

        with get_session() as session:
            article = session.get(Article, article_id)
            if article is None:
                raise SehatyNotFoundError(f"no article {article_id}")
            if article.status == ArticleStatus.REJECTED:
                # Nothing to stand behind: the text is out of circulation.
                raise SehatyValidationError("this article was rejected")

            user = session.get(User, doctor_id)
            if user is None or user.role != UserRole.DOCTOR:
                raise SehatyForbiddenError("only doctors may validate articles")
            profile = session.get(DoctorProfile, doctor_id)
            if profile is None:
                raise SehatyNotFoundError(f"no doctor profile for user {doctor_id}")
            if profile.claim_status == ClaimStatus.UNCLAIMED:
                raise SehatyForbiddenError("claim your page before validating articles")

            existing = session.execute(
                select(ArticleValidation).where(
                    ArticleValidation.article_id == article_id,
                    ArticleValidation.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    ArticleValidation(
                        article_id=article_id,
                        doctor_id=doctor_id,
                        verdict=chosen,
                        note=note,
                    )
                )
            else:
                existing.verdict = chosen
                existing.note = note
            session.flush()

        return ArticleController.get(article_id)

    @staticmethod
    def submit(article_id: int, author_id: int) -> ArticleView:
        """Hand a draft to review. The author cannot publish their own answer."""
        with get_session() as session:
            article = session.get(Article, article_id)
            if article is None:
                raise SehatyNotFoundError(f"no article {article_id}")
            if article.author_id != author_id:
                raise SehatyForbiddenError("not your article")
            if article.status == ArticleStatus.PUBLISHED:
                raise SehatyValidationError("already published")
            article.status = ArticleStatus.PENDING
            article.review_note = None
            session.flush()
        return ArticleController.get(article_id)

    @staticmethod
    def review(
        article_id: int, *, approve: bool, note: str | None = None, now: datetime | None = None
    ) -> ArticleView:
        """Publish it, or turn it down with a reason.

        Rejection keeps the text: the author edits and resubmits rather than
        starting again, which is the difference between a review and a wall.
        """
        now = now or datetime.now(UTC)
        with get_session() as session:
            article = session.get(Article, article_id)
            if article is None:
                raise SehatyNotFoundError(f"no article {article_id}")
            if approve:
                article.status = ArticleStatus.PUBLISHED
                # Set once: re-approving an edited answer must not present old
                # writing as new to a reader or a crawler.
                article.published_at = article.published_at or now
                article.review_note = None
            else:
                if not (note or "").strip():
                    raise SehatyValidationError("a rejection needs a reason")
                article.status = ArticleStatus.REJECTED
                # The guard above already rejected an empty note; mypy cannot
                # see that through `(note or "")`, and narrowing it here is
                # cheaper than restating the check.
                article.review_note = (note or "").strip()
            session.flush()
        return ArticleController.get(article_id)

    @staticmethod
    def record_event(
        slug: str, *, event: str, source: str | None = None, doctor_id: int | None = None
    ) -> None:
        """Note that something happened to a published article.

        Silent on an unknown or unpublished slug rather than raising: this is
        called from a fire-and-forget beacon, and a reader's page must never fail
        because analytics did.

        `source` is a coarse channel ("google", "whatsapp") and never a referring
        URL — a search query typed before landing on an article about depression
        is health data about whoever typed it.
        """
        try:
            kind = ArticleEventType(event)
        except ValueError:
            return

        with get_session() as session:
            article_id = session.execute(
                select(Article.id).where(
                    Article.slug == slug, Article.status == ArticleStatus.PUBLISHED
                )
            ).scalar_one_or_none()
            if article_id is None:
                return
            session.add(
                ArticleEvent(
                    article_id=article_id,
                    type=kind,
                    doctor_id=doctor_id,
                    source=(source or None) and source[:32],
                )
            )
            session.flush()

    @staticmethod
    def traffic(days: int = 30, limit: int = 100) -> list[ArticleTraffic]:
        """What each article did, by channel, over the period.

        This is the answer to "which topics should we write more of" — a question
        that was being settled by published disease prevalence, which measures who
        is ill rather than who is searching. `doctor_clicks` is separate because
        an article that is read and one that sends readers to a doctor are
        different kinds of success, and only the second pays for itself.
        """
        since = datetime.now(UTC) - timedelta(days=days)
        with get_session() as session:
            rows = session.execute(
                select(
                    Article.slug,
                    Article.title,
                    Article.locale,
                    Article.specialty_slug,
                    func.count(ArticleEvent.id).label("events"),
                    func.sum(
                        case((ArticleEvent.type == ArticleEventType.PAGE_VIEW, 1), else_=0)
                    ).label("views"),
                    func.sum(
                        case((ArticleEvent.type == ArticleEventType.DOCTOR_CLICK, 1), else_=0)
                    ).label("doctor_clicks"),
                    func.sum(case((ArticleEvent.source == "google", 1), else_=0)).label("google"),
                    func.sum(case((ArticleEvent.source == "whatsapp", 1), else_=0)).label(
                        "whatsapp"
                    ),
                    func.sum(case((ArticleEvent.source == "facebook", 1), else_=0)).label(
                        "facebook"
                    ),
                )
                .join(ArticleEvent, ArticleEvent.article_id == Article.id)
                .where(ArticleEvent.occurred_at >= since)
                .group_by(Article.slug, Article.title, Article.locale, Article.specialty_slug)
                .order_by(func.count(ArticleEvent.id).desc())
                .limit(limit)
            ).all()

        return [
            ArticleTraffic(
                slug=r.slug,
                title=r.title,
                locale=r.locale,
                specialty_slug=r.specialty_slug,
                views=int(r.views or 0),
                doctor_clicks=int(r.doctor_clicks or 0),
                from_google=int(r.google or 0),
                from_whatsapp=int(r.whatsapp or 0),
                from_facebook=int(r.facebook or 0),
            )
            for r in rows
        ]

    @staticmethod
    def voter_key(article_id: int, fingerprint: str) -> str:
        """A stable, anonymous key for one reader on one article.

        Salted and hashed, never reversible to the request it came from, and
        scoped per article so the same reader cannot be followed across the site
        by comparing keys. That last property is the point: without it, the vote
        table would quietly become a record of which articles one person read,
        which on health pages is the most sensitive thing we could hold.

        The caller supplies the fingerprint — the transport layer knows what a
        request looks like and this layer should not.
        """
        material = f"{config.VOTE_SALT}:{article_id}:{fingerprint}"
        return hashlib.sha256(material.encode()).hexdigest()

    @staticmethod
    def vote(slug: str, *, fingerprint: str, helpful: bool) -> ArticleView:
        """Record one reader's answer to "did this help you?".

        Only on a published article: a draft has no readers, and a vote on one
        would be a staff opinion wearing a reader's clothes.

        Changing your mind replaces your vote rather than adding a second, so the
        total is a count of people. Anything else and a single reader clicking
        twice would look like agreement.
        """
        with get_session() as session:
            article = session.execute(
                select(Article.id).where(
                    Article.slug == slug, Article.status == ArticleStatus.PUBLISHED
                )
            ).scalar_one_or_none()
            if article is None:
                raise SehatyNotFoundError(f"no published article {slug!r}")

            key = ArticleController.voter_key(article, fingerprint)
            existing = session.execute(
                select(ArticleVote).where(
                    ArticleVote.article_id == article, ArticleVote.voter_key == key
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(ArticleVote(article_id=article, voter_key=key, helpful=helpful))
            else:
                existing.helpful = helpful
            session.flush()

        return ArticleController.get_published(slug)

    @staticmethod
    def get(article_id: int) -> ArticleView:
        with get_session() as session:
            row = session.execute(_SELECT.where(Article.id == article_id)).one_or_none()
            if row is None:
                raise SehatyNotFoundError(f"no article {article_id}")
            signed = _validations_for(session, [row.id])
            votes = _votes_for(session, [row.id])
            others = _translations_for(session, row)
        return _view(row, signed.get(row.id), votes.get(row.id, (0, 0)), others)

    @staticmethod
    def get_published(slug: str) -> ArticleView:
        """Public read. A draft or a rejection is a 404, not a preview."""
        with get_session() as session:
            row = session.execute(
                _SELECT.where(Article.slug == slug, Article.status == ArticleStatus.PUBLISHED)
            ).one_or_none()
            if row is None:
                raise SehatyNotFoundError(f"no published article {slug!r}")
            signed = _validations_for(session, [row.id])
            votes = _votes_for(session, [row.id])
            others = _translations_for(session, row)
        return _view(row, signed.get(row.id), votes.get(row.id, (0, 0)), others)

    @staticmethod
    def list_published(
        *, specialty_slug: str | None = None, locale: str | None = None, limit: int = 50
    ) -> list[ArticleView]:
        stmt = (
            _SELECT.where(Article.status == ArticleStatus.PUBLISHED)
            .order_by(Article.published_at.desc().nullslast())
            .limit(limit)
        )
        if specialty_slug:
            stmt = stmt.where(Article.specialty_slug == specialty_slug)
        if locale:
            stmt = stmt.where(Article.locale == locale)
        with get_session() as session:
            rows = session.execute(stmt).all()
            signed = _validations_for(session, [r.id for r in rows])
            votes = _votes_for(session, [r.id for r in rows])
        return [_view(r, signed.get(r.id), votes.get(r.id, (0, 0))) for r in rows]

    @staticmethod
    def list_for_author(author_id: int) -> list[ArticleView]:
        with get_session() as session:
            rows = session.execute(
                _SELECT.where(Article.author_id == author_id).order_by(Article.id.desc())
            ).all()
            signed = _validations_for(session, [r.id for r in rows])
            votes = _votes_for(session, [r.id for r in rows])
        return [_view(r, signed.get(r.id), votes.get(r.id, (0, 0))) for r in rows]

    @staticmethod
    def list_admin(
        *,
        status: str | None = None,
        locale: str | None = None,
        specialty_slug: str | None = None,
        search: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ArticleView]:
        """Every article an admin can act on, newest first.

        `list_pending` answers one question — what is waiting on a human. An
        editor needs the other one: everything, filterable. Newest first,
        because the thing you just wrote is the thing you are looking for.
        """
        query = _SELECT
        if status:
            query = query.where(Article.status == ArticleStatus(status))
        if locale:
            query = query.where(Article.locale == locale)
        if specialty_slug:
            query = query.where(Article.specialty_slug == specialty_slug)
        if search and search.strip():
            like = f"%{search.strip()}%"
            query = query.where(Article.title.ilike(like) | Article.body.ilike(like))

        with get_session() as session:
            rows = session.execute(
                query.order_by(Article.id.desc()).limit(limit).offset(offset)
            ).all()
            signed = _validations_for(session, [r.id for r in rows])
            votes = _votes_for(session, [r.id for r in rows])
        return [_view(r, signed.get(r.id), votes.get(r.id, (0, 0))) for r in rows]

    @staticmethod
    def edit(
        article_id: int,
        *,
        title: str | None = None,
        summary: str | None = None,
        body: str | None = None,
        locale: str | None = None,
        specialty_slug: str | None = None,
        images: list[dict] | None = None,
        sources: list[dict] | None = None,
        topic_key: str | None = None,
    ) -> ArticleView:
        """Change an article. Only the fields passed are touched.

        **Editing the body discards the doctors' validations.** A doctor put
        their name to particular words; once those words change their signature
        vouches for text they never read, and they are the one person who cannot
        find that out. Losing a validation is an inconvenience, and it is the
        cheaper of the two mistakes.

        The article stays published if it was published — silently pulling live
        content offline is a surprise of its own — but it renders with no
        reviewer until someone signs it again, which is the truth.

        The slug never changes. It is printed on brochures and indexed by search
        engines, so a title corrected for a typo must not move the page.
        """
        with get_session() as session:
            article = session.get(Article, article_id)
            if article is None:
                raise SehatyNotFoundError(f"no article {article_id}")

            if title is not None or body is not None or locale is not None:
                _check_text(
                    title=title if title is not None else article.title,
                    body=body if body is not None else article.body,
                    locale=locale if locale is not None else article.locale,
                )

            body_changed = body is not None and body.strip() != article.body
            if title is not None:
                article.title = title.strip()
            if summary is not None:
                article.summary = summary.strip() or None
            if body is not None:
                article.body = body.strip()
            if locale is not None:
                article.locale = locale
            if specialty_slug is not None:
                article.specialty_slug = specialty_slug or None
            if topic_key is not None:
                article.topic_key = topic_key.strip() or None
            if images is not None:
                article.images = [i for i in images if isinstance(i, dict)]
            if sources is not None:
                cited = [
                    {"work": str(s["work"]).strip(), "locator": (s.get("locator") or None)}
                    for s in sources
                    if isinstance(s, dict) and str(s.get("work", "")).strip()
                ]
                if not cited and article.author_id is None:
                    raise SehatyValidationError(
                        "a platform-written article must cite at least one source"
                    )
                article.sources = cited

            if body_changed:
                session.query(ArticleValidation).filter(
                    ArticleValidation.article_id == article_id
                ).delete(synchronize_session=False)

            session.flush()

        return ArticleController.get(article_id)

    @staticmethod
    def delete(article_id: int) -> None:
        """Remove an article and the validations attached to it.

        A published article's URL may be indexed and linked; deleting it turns
        that into a 404 rather than anything a reader can act on. That is the
        caller's decision to make rather than this function's to refuse, but it
        is worth knowing before making it.
        """
        with get_session() as session:
            article = session.get(Article, article_id)
            if article is None:
                raise SehatyNotFoundError(f"no article {article_id}")
            session.query(ArticleValidation).filter(
                ArticleValidation.article_id == article_id
            ).delete(synchronize_session=False)
            session.delete(article)

    @staticmethod
    def list_pending(limit: int = 100) -> list[ArticleView]:
        """The review queue."""
        with get_session() as session:
            rows = session.execute(
                _SELECT.where(Article.status == ArticleStatus.PENDING)
                .order_by(Article.id.asc())
                .limit(limit)
            ).all()
            # The reviewer wants to know who has already signed it before
            # deciding whether it is publishable.
            signed = _validations_for(session, [r.id for r in rows])
            votes = _votes_for(session, [r.id for r in rows])
        return [_view(r, signed.get(r.id), votes.get(r.id, (0, 0))) for r in rows]

    @staticmethod
    def count_published_by_author(author_id: int) -> int:
        with get_session() as session:
            return int(
                session.execute(
                    select(func.count(Article.id)).where(
                        Article.author_id == author_id,
                        Article.status == ArticleStatus.PUBLISHED,
                    )
                ).scalar_one()
            )
