"""Export and restore, and the one rule that makes restore safe.

What is worth backing up is not the listings — those can be re-scraped in an
afternoon. It is the work done in cabinets: the exact pins someone stood outside
to drop, the presentation a doctor dictated, the tariff, the hours, the claims.

Hence the rule these tests exist for: a restore never overwrites something with
nothing. An operator who restores last week's file after a morning of visits
must not lose the morning.
"""

from datetime import UTC, datetime

import pytest
from geoalchemy2.elements import WKTElement
from sehaty.db import ClaimStatus, DoctorProfile, GeoPrecision, User, UserRole
from sqlalchemy import update
from sqlalchemy.orm import Session

from sehaty.core.controllers.backup import SCHEMA_VERSION, BackupController

_LAT, _LNG, _SRID = 33.5731104, -7.5898434, 4326


def _doctor(session: Session, *, email: str, slug: str, **kw) -> int:
    user = User(email=email, role=UserRole.DOCTOR, is_active=True)
    session.add(user)
    session.commit()
    session.add(
        DoctorProfile(
            user_id=user.id,
            full_name=kw.pop("full_name", "Dr Test"),
            slug=slug,
            license_no=f"LIC-{user.id}",
            city="Casablanca",
            **kw,
        )
    )
    session.commit()
    return int(user.id)


@pytest.mark.usefixtures("_pg_engine")
class TestExport:
    def test_carries_the_work_that_cannot_be_re_scraped(self, pg_session: Session) -> None:
        uid = _doctor(
            pg_session,
            email="rich@c.ma",
            slug="dr-rich",
            address="12 bd Zerktouni",
            consultation_fee=250.0,
            bio_i18n={"fr": "Cabinet au Maârif.", "ar": "عيادة في المعاريف."},
            insurances=["cnss", "amo"],
            tiers_payant=True,
            geopoint=WKTElement(f"POINT({_LNG} {_LAT})", srid=_SRID),
            geo_precision=GeoPrecision.EXACT,
        )
        assert uid

        backup = BackupController.export()

        (row,) = [r for r in backup.rows if r.slug == "dr-rich"]
        assert backup.schema_version == SCHEMA_VERSION
        assert row.consultation_fee == 250.0
        assert row.bio_i18n["ar"] == "عيادة في المعاريف."
        assert row.insurances == ["cnss", "amo"]
        assert row.tiers_payant is True
        assert row.geo_precision == "EXACT"
        assert row.lat == pytest.approx(_LAT, abs=1e-6)


@pytest.mark.usefixtures("_pg_engine")
class TestRestore:
    def _payload(self, **row) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "count": 1,
            "rows": [row],
        }

    def test_a_blank_in_the_backup_never_clears_a_value(self, pg_session: Session) -> None:
        """The rule the whole feature turns on.

        Restoring last week's file after a morning of visits must not undo the
        morning.
        """
        _doctor(
            pg_session,
            email="keep@c.ma",
            slug="dr-keep",
            address="12 bd Zerktouni",
            consultation_fee=250.0,
        )

        BackupController.restore(
            self._payload(slug="dr-keep", address=None, consultation_fee=None, phone_fixe=""),
            dry_run=False,
        )

        with pg_session.begin():
            kept = pg_session.query(DoctorProfile).filter_by(slug="dr-keep").one()
            assert kept.address == "12 bd Zerktouni"
            assert kept.consultation_fee == 250.0

    def test_it_restores_what_the_backup_actually_has(self, pg_session: Session) -> None:
        _doctor(pg_session, email="thin@c.ma", slug="dr-thin")

        report = BackupController.restore(
            self._payload(
                slug="dr-thin",
                address="45 rue Ibn Batouta",
                consultation_fee=300.0,
                bio_i18n={"fr": "Rétabli."},
            ),
            dry_run=False,
        )

        assert report.updated == 1
        with pg_session.begin():
            restored = pg_session.query(DoctorProfile).filter_by(slug="dr-thin").one()
            assert restored.address == "45 rue Ibn Batouta"
            assert restored.bio_i18n == {"fr": "Rétabli."}

    def test_an_exact_pin_is_never_replaced_by_an_approximate_one(
        self, pg_session: Session
    ) -> None:
        """The most expensive field in the file: someone stood outside for it."""
        _doctor(
            pg_session,
            email="pin@c.ma",
            slug="dr-pin",
            geopoint=WKTElement(f"POINT({_LNG} {_LAT})", srid=_SRID),
            geo_precision=GeoPrecision.EXACT,
        )

        BackupController.restore(
            self._payload(slug="dr-pin", lat=33.9, lng=-6.9, geo_precision="APPROXIMATE"),
            dry_run=False,
        )

        with pg_session.begin():
            kept = pg_session.query(DoctorProfile).filter_by(slug="dr-pin").one()
            assert kept.geo_precision == GeoPrecision.EXACT

    def test_a_delisted_doctor_is_never_restored(self, pg_session: Session) -> None:
        """A tombstone a backup can walk back is not a tombstone."""
        uid = _doctor(pg_session, email="gone@c.ma", slug="dr-gone")
        with pg_session.begin():
            pg_session.execute(
                update(DoctorProfile)
                .where(DoctorProfile.user_id == uid)
                .values(claim_status=ClaimStatus.REMOVAL_REQUESTED)
            )

        report = BackupController.restore(
            self._payload(slug="dr-gone", address="somewhere"), dry_run=False
        )

        assert report.skipped_removed == 1
        with pg_session.begin():
            assert pg_session.query(DoctorProfile).filter_by(slug="dr-gone").one().address is None

    def test_a_dry_run_changes_nothing(self, pg_session: Session) -> None:
        _doctor(pg_session, email="dry@c.ma", slug="dr-dry")

        report = BackupController.restore(
            self._payload(slug="dr-dry", address="45 rue Ibn Batouta"), dry_run=True
        )

        assert report.updated == 1  # it says what it *would* do
        with pg_session.begin():
            assert pg_session.query(DoctorProfile).filter_by(slug="dr-dry").one().address is None

    def test_a_wrong_schema_version_is_refused_whole(self, pg_session: Session) -> None:
        """Half-applying a file we do not understand is worse than refusing it."""
        report = BackupController.restore(
            {"schema_version": 99, "rows": [{"slug": "dr-x"}]}, dry_run=False
        )

        assert report.errors and "schema_version" in report.errors[0]
        assert report.updated == 0

    def test_an_unknown_slug_is_reported_not_invented(self, pg_session: Session) -> None:
        report = BackupController.restore(self._payload(slug="dr-nobody"), dry_run=False)

        assert report.errors == ["dr-nobody: not in the directory, skipped"]


@pytest.mark.usefixtures("_pg_engine")
def test_replaying_a_fresh_backup_changes_nothing(pg_session: Session) -> None:
    """The round trip, where it can actually run.

    Restoring a file taken seconds ago must be a no-op. If it reports changes,
    the export and the restore disagree about something, and the first anyone
    would learn of it is a restore that quietly rewrote the directory.
    """
    _doctor(
        pg_session,
        email="round@c.ma",
        slug="dr-round",
        address="12 bd Zerktouni",
        consultation_fee=250.0,
        bio_i18n={"fr": "Cabinet au Maârif."},
        insurances=["cnss"],
        tiers_payant=True,
        geopoint=WKTElement(f"POINT({_LNG} {_LAT})", srid=_SRID),
        geo_precision=GeoPrecision.EXACT,
    )

    exported = BackupController.export()
    report = BackupController.restore(exported.model_dump(mode="json"), dry_run=False)

    assert report.updated == 0
    assert report.unchanged == exported.count
    assert report.errors == []
