"""Exporting the directory, and putting it back.

What a backup is *for* here is narrow and worth being precise about. The 5,000
listings can be re-scraped in an afternoon; what cannot be recovered is the work
done in cabinets — the exact pins someone stood outside to drop, the presentation
a doctor dictated in three languages, the tariff, the hours, and which pages have
been claimed. Losing that costs petrol and appointments, not an afternoon.

So the export carries the whole profile rather than the scrape columns, and the
restore is built around one rule: **it never overwrites something with nothing.**
A blank field in a backup means "this was empty then", not "delete what is there
now" — and an operator restoring last week's file after a morning of visits must
not undo the morning.

Removals are honoured in both directions. A doctor who asked to be delisted is
skipped on restore however old the file is, because a tombstone that a backup can
walk back is not a tombstone.
"""

from __future__ import annotations

from datetime import UTC, datetime

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKTElement
from sehaty.db import (
    ClaimStatus,
    DoctorLanding,
    DoctorProfile,
    DoctorSpecialty,
    Specialty,
)
from sqlalchemy import cast, func, select

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session

SCHEMA_VERSION = 1
_SRID = 4326


class BackupRow(DomainModel):
    """One doctor, as much of them as is worth restoring."""

    slug: str
    full_name: str
    specialty_slugs: list[str]
    city: str | None
    district: str | None
    address: str | None
    phone_fixe: str | None
    phone_mobile: str | None
    whatsapp: str | None
    lat: float | None
    lng: float | None
    geo_precision: str | None
    bio: str | None
    bio_i18n: dict
    consultation_fee: float | None
    languages: list[str]
    opening_hours: list
    insurances: list[str]
    tiers_payant: bool
    timezone: str | None
    claim_status: str
    verification_status: str
    source: str | None
    is_personalized: bool


class BackupFile(DomainModel):
    """The export, with enough context to know what you are restoring."""

    schema_version: int
    exported_at: datetime
    count: int
    rows: list[BackupRow]


class RestoreReport(DomainModel):
    """What a restore actually did, stated plainly enough to act on."""

    total: int
    created: int
    updated: int
    skipped_removed: int
    unchanged: int
    errors: list[str]


class BackupController:
    @staticmethod
    def export(city: str | None = None) -> BackupFile:
        """Everything worth keeping, optionally for one city."""
        specialties = (
            select(
                DoctorSpecialty.doctor_id.label("doctor_id"),
                func.array_agg(Specialty.slug).label("slugs"),
            )
            .join(Specialty, Specialty.id == DoctorSpecialty.specialty_id)
            .group_by(DoctorSpecialty.doctor_id)
            .subquery()
        )
        stmt = (
            select(
                DoctorProfile,
                func.ST_Y(cast(DoctorProfile.geopoint, Geometry)).label("lat"),
                func.ST_X(cast(DoctorProfile.geopoint, Geometry)).label("lng"),
                specialties.c.slugs,
                DoctorLanding.is_personalized,
            )
            .outerjoin(specialties, specialties.c.doctor_id == DoctorProfile.user_id)
            .outerjoin(DoctorLanding, DoctorLanding.doctor_id == DoctorProfile.user_id)
            .order_by(DoctorProfile.slug.asc())
        )
        if city:
            stmt = stmt.where(DoctorProfile.city == city)

        with get_session() as session:
            records = session.execute(stmt).all()

        rows = [
            BackupRow(
                slug=r.DoctorProfile.slug,
                full_name=r.DoctorProfile.full_name,
                specialty_slugs=list(r.slugs or []),
                city=r.DoctorProfile.city,
                district=r.DoctorProfile.district,
                address=r.DoctorProfile.address,
                phone_fixe=r.DoctorProfile.phone_fixe,
                phone_mobile=r.DoctorProfile.phone_mobile,
                whatsapp=r.DoctorProfile.whatsapp,
                lat=r.lat,
                lng=r.lng,
                geo_precision=(
                    str(r.DoctorProfile.geo_precision) if r.DoctorProfile.geo_precision else None
                ),
                bio=r.DoctorProfile.bio,
                bio_i18n=r.DoctorProfile.bio_i18n or {},
                consultation_fee=r.DoctorProfile.consultation_fee,
                languages=list(r.DoctorProfile.languages or []),
                opening_hours=list(r.DoctorProfile.opening_hours or []),
                insurances=list(r.DoctorProfile.insurances or []),
                tiers_payant=bool(r.DoctorProfile.tiers_payant),
                timezone=r.DoctorProfile.timezone,
                claim_status=str(r.DoctorProfile.claim_status),
                verification_status=str(r.DoctorProfile.verification_status),
                source=str(r.DoctorProfile.source) if r.DoctorProfile.source else None,
                is_personalized=bool(r.is_personalized),
            )
            for r in records
        ]
        return BackupFile(
            schema_version=SCHEMA_VERSION,
            exported_at=datetime.now(UTC),
            count=len(rows),
            rows=rows,
        )

    @staticmethod
    def restore(payload: dict, *, dry_run: bool = True) -> RestoreReport:
        """Put an export back, without undoing work done since.

        Matched on slug, which is the one identifier that is stable across a
        re-import and printed on plaques.

        The rule throughout: a blank in the file never clears a value in the
        database. A backup says what was known then, and an operator restoring
        last week's file after a morning of visits must not lose the morning.
        """
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            return RestoreReport(
                total=0,
                created=0,
                updated=0,
                skipped_removed=0,
                unchanged=0,
                errors=[f"unsupported schema_version {version!r}, expected {SCHEMA_VERSION}"],
            )

        rows = payload.get("rows") or []
        # Plain counters rather than a mutable report: the projections are frozen
        # on purpose, and a half-built report handed around mid-loop is exactly
        # the sort of thing that ends up reported wrong.
        updated = unchanged = skipped_removed = 0
        errors: list[str] = []

        with get_session() as session:
            for index, row in enumerate(rows, 1):
                slug = (row.get("slug") or "").strip()
                if not slug:
                    errors.append(f"row {index}: no slug")
                    continue

                found = session.execute(
                    select(
                        DoctorProfile,
                        func.ST_Y(cast(DoctorProfile.geopoint, Geometry)).label("lat"),
                        func.ST_X(cast(DoctorProfile.geopoint, Geometry)).label("lng"),
                    ).where(DoctorProfile.slug == slug)
                ).one_or_none()
                profile = found.DoctorProfile if found else None

                if profile is None:
                    # Creating from a backup would need a user row and an email,
                    # and inventing an account for a doctor no longer in the
                    # database is not a restore — it is a resurrection with no
                    # audit trail. Reported rather than done.
                    errors.append(f"{slug}: not in the directory, skipped")
                    continue

                if profile.claim_status == ClaimStatus.REMOVAL_REQUESTED:
                    # A tombstone a backup can walk back is not a tombstone.
                    skipped_removed += 1
                    continue

                if _apply(profile, row, current=(found.lat, found.lng), dry_run=dry_run):
                    updated += 1
                else:
                    unchanged += 1

            if dry_run:
                session.rollback()
            else:
                session.flush()

        return RestoreReport(
            total=len(rows),
            created=0,
            updated=updated,
            skipped_removed=skipped_removed,
            unchanged=unchanged,
            errors=errors,
        )


# Fields restored only when the backup has something and the database does not,
# or when the backup's value differs and is non-empty. Never blanked.
_SIMPLE = (
    "full_name",
    "city",
    "district",
    "address",
    "phone_fixe",
    "phone_mobile",
    "whatsapp",
    "bio",
    "consultation_fee",
    "timezone",
)


def _apply(
    profile: DoctorProfile,
    row: dict,
    *,
    current: tuple[float | None, float | None],
    dry_run: bool,
) -> bool:
    """Copy what the backup knows onto the profile. Returns whether anything moved."""
    changed = False

    for field in _SIMPLE:
        value = row.get(field)
        if value in (None, "", []):
            continue  # a blank is "unknown then", never "delete now"
        if getattr(profile, field) != value:
            if not dry_run:
                setattr(profile, field, value)
            changed = True

    for field in ("languages", "opening_hours", "insurances", "bio_i18n"):
        value = row.get(field)
        if not value:
            continue
        if getattr(profile, field) != value:
            if not dry_run:
                setattr(profile, field, value)
            changed = True

    if row.get("tiers_payant") and not profile.tiers_payant:
        if not dry_run:
            profile.tiers_payant = True
        changed = True

    lat, lng = row.get("lat"), row.get("lng")
    precision = row.get("geo_precision")
    if lat is not None and lng is not None:
        current_lat, current_lng = current
        stored_precision = str(profile.geo_precision) if profile.geo_precision else None
        # Compare the point, not just the precision. Without this, replaying a
        # backup taken seconds ago reports every EXACT pin as changed — the
        # restore looks like it rewrote the directory when it did nothing.
        same_point = (
            current_lat is not None
            and current_lng is not None
            and abs(current_lat - float(lat)) < 1e-7
            and abs(current_lng - float(lng)) < 1e-7
            and stored_precision == precision
        )
        # An EXACT pin someone stood outside to drop is the most expensive thing
        # in this file. It replaces an approximate one, and never the reverse.
        may_replace = stored_precision != "EXACT" or precision == "EXACT"
        if not same_point and may_replace:
            if not dry_run:
                profile.geopoint = WKTElement(f"POINT({lng} {lat})", srid=_SRID)
                if precision:
                    profile.geo_precision = precision
            changed = True

    return changed
