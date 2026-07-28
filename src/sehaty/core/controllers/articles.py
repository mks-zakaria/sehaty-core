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

import re
import unicodedata
from datetime import UTC, datetime

from sehaty.db import (
    Article,
    ArticleStatus,
    ClaimStatus,
    DoctorProfile,
    User,
    UserRole,
)
from sqlalchemy import func, select

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


class ArticleView(DomainModel):
    """One answer, with the byline a reader needs to weigh it."""

    id: int
    slug: str
    title: str
    summary: str | None
    body: str
    locale: str
    specialty_slug: str | None
    status: str
    published_at: datetime | None
    review_note: str | None

    author_id: int
    author_name: str | None
    # Links the answer back to its author's page — the internal link that makes
    # this worth writing for the doctor and worth having for the directory.
    author_slug: str | None
    author_city: str | None


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


def _view(row) -> ArticleView:  # noqa: ANN001
    return ArticleView(
        id=row.id,
        slug=row.slug,
        title=row.title,
        summary=row.summary,
        body=row.body,
        locale=row.locale,
        specialty_slug=row.specialty_slug,
        status=str(row.status),
        published_at=row.published_at,
        review_note=row.review_note,
        author_id=row.author_id,
        author_name=getattr(row, "full_name", None),
        author_slug=getattr(row, "author_slug", None),
        author_city=getattr(row, "city", None),
    )


_SELECT = select(
    Article.id,
    Article.slug,
    Article.title,
    Article.summary,
    Article.body,
    Article.locale,
    Article.specialty_slug,
    Article.status,
    Article.published_at,
    Article.review_note,
    Article.author_id,
    DoctorProfile.full_name,
    DoctorProfile.slug.label("author_slug"),
    DoctorProfile.city,
).join(DoctorProfile, DoctorProfile.user_id == Article.author_id, isouter=True)


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
                title=title,
                slug=_unique_slug(session, article_slug(title)),
                summary=(summary or "").strip() or None,
                body=body,
                locale=locale,
                specialty_slug=specialty_slug,
                status=ArticleStatus.DRAFT,
            )
            session.add(article)
            session.flush()
            article_id = article.id

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
                article.review_note = note.strip()
            session.flush()
        return ArticleController.get(article_id)

    @staticmethod
    def get(article_id: int) -> ArticleView:
        with get_session() as session:
            row = session.execute(_SELECT.where(Article.id == article_id)).one_or_none()
        if row is None:
            raise SehatyNotFoundError(f"no article {article_id}")
        return _view(row)

    @staticmethod
    def get_published(slug: str) -> ArticleView:
        """Public read. A draft or a rejection is a 404, not a preview."""
        with get_session() as session:
            row = session.execute(
                _SELECT.where(Article.slug == slug, Article.status == ArticleStatus.PUBLISHED)
            ).one_or_none()
        if row is None:
            raise SehatyNotFoundError(f"no published article {slug!r}")
        return _view(row)

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
            return [_view(r) for r in session.execute(stmt).all()]

    @staticmethod
    def list_for_author(author_id: int) -> list[ArticleView]:
        with get_session() as session:
            rows = session.execute(
                _SELECT.where(Article.author_id == author_id).order_by(Article.id.desc())
            ).all()
        return [_view(r) for r in rows]

    @staticmethod
    def list_pending(limit: int = 100) -> list[ArticleView]:
        """The review queue."""
        with get_session() as session:
            rows = session.execute(
                _SELECT.where(Article.status == ArticleStatus.PENDING)
                .order_by(Article.id.asc())
                .limit(limit)
            ).all()
        return [_view(r) for r in rows]

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
