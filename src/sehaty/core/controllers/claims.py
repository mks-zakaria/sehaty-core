"""Claiming and delisting a published doctor page.

Pages are published from public professional data before the doctor is ever
contacted. Publishing a professional's practice listing is defensible; doing it
with no way for them to correct or remove it is not. This module is that way.

Two deliberate properties:

* A removal request takes effect **immediately** — the page stops being publicly
  visible on the same request, before any human reviews it. Making someone wait
  while their listing stays up gets the order exactly backwards.
* A removal is a **tombstone**, not a delete. ``REMOVAL_REQUESTED`` persists so a
  later import cannot silently republish the same doctor from the same public
  source they already objected to.
"""

from datetime import UTC, datetime

from sehaty.db import ClaimStatus, DoctorProfile, VerificationStatus
from sqlalchemy import select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError


class ClaimRequestResult(DomainModel):
    """Outcome of a claim or removal request."""

    slug: str
    claim_status: str
    # True when the page is no longer publicly visible as a result.
    delisted: bool


class ClaimController:
    @staticmethod
    def request_removal(slug: str, *, reason: str | None = None) -> ClaimRequestResult:
        """Delist a doctor's page immediately at their request.

        Sets ``verification_status`` to REJECTED so every public read
        (``get_by_slug``, the directory, the city pages, the sitemap) stops
        surfacing it at once, and records the request timestamp for audit.

        Idempotent: asking twice is not an error, because a doctor chasing a
        removal should never be told their second request failed.
        """
        if not slug or not slug.strip():
            raise SehatyValidationError("slug is required")

        with get_session() as session:
            profile = session.execute(
                select(DoctorProfile).where(DoctorProfile.slug == slug.strip())
            ).scalar_one_or_none()
            if profile is None:
                raise SehatyNotFoundError(f"no doctor for slug {slug!r}")

            profile.claim_status = ClaimStatus.REMOVAL_REQUESTED
            profile.removal_requested_at = datetime.now(UTC)
            # Take it down now; review afterwards.
            profile.verification_status = VerificationStatus.REJECTED
            if reason and reason.strip():
                note = f"[removal] {reason.strip()[:400]}"
                profile.bio = note if not profile.bio else f"{profile.bio}\n{note}"

            return ClaimRequestResult(
                slug=profile.slug,
                claim_status=str(profile.claim_status),
                delisted=True,
            )

    @staticmethod
    def mark_claimed(slug: str) -> ClaimRequestResult:
        """Record that the doctor has taken ownership of an unclaimed page.

        Does **not** grant verification — that requires checking the licence, and
        conflating "someone said this is mine" with "we confirmed who they are"
        would put a trust badge on an unverified claim. A page whose removal was
        requested cannot be claimed back through this path; that needs a human.
        """
        if not slug or not slug.strip():
            raise SehatyValidationError("slug is required")

        with get_session() as session:
            profile = session.execute(
                select(DoctorProfile).where(DoctorProfile.slug == slug.strip())
            ).scalar_one_or_none()
            if profile is None:
                raise SehatyNotFoundError(f"no doctor for slug {slug!r}")
            if profile.claim_status == ClaimStatus.REMOVAL_REQUESTED:
                raise SehatyValidationError(
                    "this page was delisted at the doctor's request; contact support"
                )

            if profile.claim_status == ClaimStatus.UNCLAIMED:
                profile.claim_status = ClaimStatus.CLAIMED

            return ClaimRequestResult(
                slug=profile.slug,
                claim_status=str(profile.claim_status),
                delisted=False,
            )

    @staticmethod
    def is_removal_requested(slug: str) -> bool:
        """Whether this slug was delisted on request — the import's do-not-publish check."""
        with get_session() as session:
            status = session.execute(
                select(DoctorProfile.claim_status).where(DoctorProfile.slug == slug)
            ).scalar_one_or_none()
        return status == ClaimStatus.REMOVAL_REQUESTED
