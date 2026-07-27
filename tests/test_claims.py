"""Pure-SQLite tests for claiming and delisting a published doctor page.

The properties that matter here are ethical, not merely functional: a removal
must take the page down *immediately* rather than queue for review, a removal
must survive a re-import, and claiming a page must never confer a verification
badge. Same dialect-scoped ``Geography`` shim as the other SQLite suites.
"""

import pytest
from geoalchemy2 import Geography
from geoalchemy2 import functions as geo_functions
from sehaty.db import (
    ClaimStatus,
    DoctorProfile,
    ProfileSource,
    User,
    UserRole,
    VerificationStatus,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.claims import ClaimController
from sehaty.core.controllers.doctors import DoctorController
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError


@compiles(Geography, "sqlite")
def _geography_as_text_on_sqlite(element, compiler, **kw) -> str:
    return "TEXT"


@compiles(geo_functions.ST_GeogFromText, "sqlite")
def _geog_bind_passthrough_on_sqlite(element, compiler, **kw) -> str:
    return compiler.process(list(element.clauses)[0], **kw)


@compiles(geo_functions.ST_AsEWKB, "sqlite")
@compiles(geo_functions.ST_AsBinary, "sqlite")
def _read_geopoint_passthrough_on_sqlite(element, compiler, **kw) -> str:
    return compiler.process(list(element.clauses)[0], **kw)


_TABLES = [User.__table__, DoctorProfile.__table__]


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


def _doctor(
    factory: sessionmaker[Session],
    slug: str,
    *,
    claim: ClaimStatus = ClaimStatus.UNCLAIMED,
) -> None:
    with factory() as s:
        user = User(email=f"{slug}@clinic.ma", role=UserRole.DOCTOR, is_active=True)
        s.add(user)
        s.flush()
        s.add(
            DoctorProfile(
                user_id=user.id,
                full_name=slug,
                slug=slug,
                license_no=f"LIC-{slug}",
                verification_status=VerificationStatus.VERIFIED,
                claim_status=claim,
                source=ProfileSource.IMPORT,
            )
        )
        s.commit()


def _profile(factory: sessionmaker[Session], slug: str) -> DoctorProfile:
    with factory() as s:
        return s.execute(select(DoctorProfile).where(DoctorProfile.slug == slug)).scalar_one()


class TestRemoval:
    def test_delists_the_page_immediately(self, db: sessionmaker[Session]) -> None:
        _doctor(db, "dr-out")

        result = ClaimController.request_removal("dr-out")

        assert result.delisted is True
        profile = _profile(db, "dr-out")
        assert profile.claim_status == ClaimStatus.REMOVAL_REQUESTED
        # Taken down now, reviewed afterwards — not the other way round.
        assert profile.verification_status == VerificationStatus.REJECTED
        assert profile.removal_requested_at is not None

    def test_the_slug_leaves_the_published_set(self, db: sessionmaker[Session]) -> None:
        # The observable "it is gone" property, checked through the same
        # eligibility filter `get_by_slug` uses. `get_by_slug` itself cannot be
        # exercised here: it projects the geopoint through ST_Y/ST_X, which stock
        # SQLite has no functions for. Its 404 path is covered against live
        # PostGIS in test_doctor_profile.py.
        _doctor(db, "dr-listed")
        assert "dr-listed" in DoctorController.list_published_slugs()

        ClaimController.request_removal("dr-listed")

        assert "dr-listed" not in DoctorController.list_published_slugs()

    def test_is_idempotent(self, db: sessionmaker[Session]) -> None:
        # A doctor chasing a removal must never be told the second try failed.
        _doctor(db, "dr-twice")
        ClaimController.request_removal("dr-twice")
        assert ClaimController.request_removal("dr-twice").delisted is True

    def test_records_the_reason_for_audit(self, db: sessionmaker[Session]) -> None:
        _doctor(db, "dr-reason")
        ClaimController.request_removal("dr-reason", reason="Je ne veux pas figurer")
        assert "[removal] Je ne veux pas figurer" in (_profile(db, "dr-reason").bio or "")

    def test_unknown_slug_raises_not_found(self, db: sessionmaker[Session]) -> None:
        with pytest.raises(SehatyNotFoundError):
            ClaimController.request_removal("nobody")

    def test_blank_slug_is_rejected(self, db: sessionmaker[Session]) -> None:
        with pytest.raises(SehatyValidationError):
            ClaimController.request_removal("   ")


class TestClaim:
    def test_marks_an_unclaimed_page_claimed(self, db: sessionmaker[Session]) -> None:
        _doctor(db, "dr-claim")

        result = ClaimController.mark_claimed("dr-claim")

        assert result.claim_status == ClaimStatus.CLAIMED
        assert _profile(db, "dr-claim").claim_status == ClaimStatus.CLAIMED

    def test_never_confers_verification(self, db: sessionmaker[Session]) -> None:
        # "Someone said this is mine" must not become a trust badge.
        _doctor(db, "dr-badge")
        ClaimController.mark_claimed("dr-badge")
        assert _profile(db, "dr-badge").claim_status != ClaimStatus.VERIFIED

    def test_does_not_downgrade_an_already_verified_claim(self, db: sessionmaker[Session]) -> None:
        _doctor(db, "dr-verified", claim=ClaimStatus.VERIFIED)
        ClaimController.mark_claimed("dr-verified")
        assert _profile(db, "dr-verified").claim_status == ClaimStatus.VERIFIED

    def test_cannot_reclaim_a_removed_page(self, db: sessionmaker[Session]) -> None:
        _doctor(db, "dr-removed")
        ClaimController.request_removal("dr-removed")

        with pytest.raises(SehatyValidationError):
            ClaimController.mark_claimed("dr-removed")


class TestRemovalTombstone:
    def test_removal_is_detectable_so_an_import_cannot_republish(
        self, db: sessionmaker[Session]
    ) -> None:
        # The tombstone is the whole point: a re-run of the importer against the
        # same public source must not quietly put the doctor back online.
        _doctor(db, "dr-tomb")
        assert ClaimController.is_removal_requested("dr-tomb") is False

        ClaimController.request_removal("dr-tomb")

        assert ClaimController.is_removal_requested("dr-tomb") is True

    def test_unknown_slug_is_not_flagged(self, db: sessionmaker[Session]) -> None:
        assert ClaimController.is_removal_requested("nobody") is False
