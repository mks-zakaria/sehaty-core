"""Practice-profile (letterhead) business logic.

Class-as-namespace with @staticmethod (the RevlyMainDBClient pattern). A doctor
keeps one or more :class:`PracticeProfile` letterheads — one per location — used
to render printable prescriptions. Exactly one profile is the *default* (the
single-default invariant): the first profile a doctor creates is forced default,
and setting one default unsets the others.

Everything here is doctor-scoped: every method takes ``doctor_id`` and filters
by it, so a profile belonging to another doctor is a
:class:`SehatyNotFoundError`. Reads return small frozen dataclass projections
(never ORM objects), so callers hold detached, serialisable data. Failures raise
the ``SehatyError`` taxonomy; methods never return ``None`` to signal an error.
All IO flows through ``get_session``.
"""

from datetime import datetime

from sehaty.db import PracticeProfile
from sqlalchemy import select, update

from sehaty.core._dto import DomainModel
from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyNotFoundError, SehatyValidationError

# Fields a doctor may set on their own profile via create/update (not is_default,
# which flows through the single-default invariant helpers).
_WRITABLE = (
    "name",
    "clinic_name",
    "address",
    "city",
    "phone",
    "header_line",
    "signature_name",
    "signature_image_url",
    "watermark_text",
    "watermark_image_url",
)


class PracticeProfileRow(DomainModel):
    """A doctor's letterhead (detached, column-only projection)."""

    id: int
    doctor_id: int
    name: str
    clinic_name: str | None
    address: str | None
    city: str | None
    phone: str | None
    header_line: str | None
    signature_name: str | None
    signature_image_url: str | None
    watermark_text: str | None
    watermark_image_url: str | None
    is_default: bool
    created_at: datetime


def _row(profile: PracticeProfile) -> PracticeProfileRow:
    """Project an ORM profile onto the detached dataclass."""
    return PracticeProfileRow(
        id=profile.id,
        doctor_id=profile.doctor_id,
        name=profile.name,
        clinic_name=profile.clinic_name,
        address=profile.address,
        city=profile.city,
        phone=profile.phone,
        header_line=profile.header_line,
        signature_name=profile.signature_name,
        signature_image_url=profile.signature_image_url,
        watermark_text=profile.watermark_text,
        watermark_image_url=profile.watermark_image_url,
        is_default=bool(profile.is_default),
        created_at=profile.created_at,
    )


class PracticeProfileController:
    @staticmethod
    def list_for(doctor_id: int) -> list[PracticeProfileRow]:
        """List the doctor's letterheads, the default first, then oldest first."""
        stmt = (
            select(PracticeProfile)
            .where(PracticeProfile.doctor_id == doctor_id)
            .order_by(PracticeProfile.is_default.desc(), PracticeProfile.id.asc())
        )
        with get_session() as session:
            rows = session.execute(stmt).scalars().all()
            return [_row(p) for p in rows]

    @staticmethod
    def create(
        doctor_id: int,
        name: str,
        clinic_name: str | None = None,
        address: str | None = None,
        city: str | None = None,
        phone: str | None = None,
        header_line: str | None = None,
        signature_name: str | None = None,
        signature_image_url: str | None = None,
        watermark_text: str | None = None,
        watermark_image_url: str | None = None,
        is_default: bool = False,
    ) -> PracticeProfileRow:
        """Create a letterhead for the doctor, upholding the single-default rule.

        ``name`` must be non-empty (else :class:`SehatyValidationError`). The
        doctor's FIRST profile is always forced default. Otherwise, when
        ``is_default`` is requested the doctor's other profiles are unset so at
        most one default ever exists. Returns the new row.
        """
        clean = (name or "").strip()
        if not clean:
            raise SehatyValidationError("name must not be empty")

        with get_session() as session:
            has_any = (
                session.execute(
                    select(PracticeProfile.id)
                    .where(PracticeProfile.doctor_id == doctor_id)
                    .limit(1)
                ).first()
                is not None
            )
            # First profile is always the default; else honour the flag.
            make_default = True if not has_any else is_default
            if make_default and has_any:
                session.execute(
                    update(PracticeProfile)
                    .where(PracticeProfile.doctor_id == doctor_id)
                    .values(is_default=False)
                )
            profile = PracticeProfile(
                doctor_id=doctor_id,
                name=clean,
                clinic_name=clinic_name,
                address=address,
                city=city,
                phone=phone,
                header_line=header_line,
                signature_name=signature_name,
                signature_image_url=signature_image_url,
                watermark_text=watermark_text,
                watermark_image_url=watermark_image_url,
                is_default=make_default,
            )
            session.add(profile)
            session.flush()
            return _row(profile)

    @staticmethod
    def update(doctor_id: int, profile_id: int, **fields: object) -> PracticeProfileRow:
        """Update the allowed fields on the doctor's own letterhead.

        Only the letterhead columns are writable; unknown keys are ignored. If
        ``is_default`` is set truthy the doctor's other profiles are unset (the
        single-default invariant); a falsy ``is_default`` is applied as given.
        A ``name`` present in ``fields`` must remain non-empty. The row must
        belong to ``doctor_id`` (else :class:`SehatyNotFoundError`). Returns the
        refreshed row.
        """
        updates = {k: v for k, v in fields.items() if k in _WRITABLE}
        if "name" in updates:
            clean = (str(updates["name"]) if updates["name"] is not None else "").strip()
            if not clean:
                raise SehatyValidationError("name must not be empty")
            updates["name"] = clean

        with get_session() as session:
            profile = session.execute(
                select(PracticeProfile).where(
                    PracticeProfile.id == profile_id,
                    PracticeProfile.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if profile is None:
                raise SehatyNotFoundError(
                    f"no practice profile {profile_id} for doctor {doctor_id}"
                )
            for key, value in updates.items():
                setattr(profile, key, value)
            if "is_default" in fields:
                if fields["is_default"]:
                    session.execute(
                        update(PracticeProfile)
                        .where(
                            PracticeProfile.doctor_id == doctor_id,
                            PracticeProfile.id != profile_id,
                        )
                        .values(is_default=False)
                    )
                    profile.is_default = True
                else:
                    profile.is_default = False
            session.flush()
            return _row(profile)

    @staticmethod
    def set_default(doctor_id: int, profile_id: int) -> PracticeProfileRow:
        """Make ``profile_id`` the doctor's one and only default letterhead.

        The row must belong to ``doctor_id`` (else :class:`SehatyNotFoundError`).
        """
        with get_session() as session:
            profile = session.execute(
                select(PracticeProfile).where(
                    PracticeProfile.id == profile_id,
                    PracticeProfile.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if profile is None:
                raise SehatyNotFoundError(
                    f"no practice profile {profile_id} for doctor {doctor_id}"
                )
            session.execute(
                update(PracticeProfile)
                .where(
                    PracticeProfile.doctor_id == doctor_id,
                    PracticeProfile.id != profile_id,
                )
                .values(is_default=False)
            )
            profile.is_default = True
            session.flush()
            return _row(profile)

    @staticmethod
    def delete(doctor_id: int, profile_id: int) -> None:
        """Delete the doctor's letterhead; promote another if it was the default.

        The row must belong to ``doctor_id`` (else :class:`SehatyNotFoundError`).
        If the deleted profile was the default and other profiles remain, the
        oldest survivor is promoted so a default still exists.
        """
        with get_session() as session:
            profile = session.execute(
                select(PracticeProfile).where(
                    PracticeProfile.id == profile_id,
                    PracticeProfile.doctor_id == doctor_id,
                )
            ).scalar_one_or_none()
            if profile is None:
                raise SehatyNotFoundError(
                    f"no practice profile {profile_id} for doctor {doctor_id}"
                )
            was_default = bool(profile.is_default)
            session.delete(profile)
            session.flush()
            if was_default:
                survivor = session.execute(
                    select(PracticeProfile)
                    .where(PracticeProfile.doctor_id == doctor_id)
                    .order_by(PracticeProfile.id.asc())
                    .limit(1)
                ).scalar_one_or_none()
                if survivor is not None:
                    survivor.is_default = True
                    session.flush()

    @staticmethod
    def get_default(doctor_id: int) -> PracticeProfileRow | None:
        """Return the doctor's default letterhead, or ``None`` if they have none."""
        with get_session() as session:
            profile = session.execute(
                select(PracticeProfile)
                .where(
                    PracticeProfile.doctor_id == doctor_id,
                    PracticeProfile.is_default.is_(True),
                )
                .order_by(PracticeProfile.id.asc())
                .limit(1)
            ).scalar_one_or_none()
            return _row(profile) if profile is not None else None
