"""Admin-tunable configuration: ranking weights + feature flags.

Class-as-namespace with ``@staticmethod`` (the RevlyMainDBClient pattern, same
as ``BillingController``/``AdminController``). Two knobs live here:

* ``RankingWeights`` — a single-row-ish table of weights the doctor-locator
  ranking formula reads live (``DoctorSearchController`` fetches them once per
  search). ``get_ranking_weights`` never returns ``None``: with no row it hands
  back the same defaults the ORM column defaults declare, so search behaviour is
  unchanged until an admin tunes them.
* ``FeatureFlag`` — named boolean toggles gating product features.

Mutations are ADMIN-only (a non-admin actor is ``SehatyForbiddenError``) and
upsert the singleton/named row. All IO flows through ``get_session``; failures
raise the ``SehatyError`` taxonomy, never a ``None`` sentinel.
"""

from dataclasses import dataclass, fields

from sehaty.db import FeatureFlag, RankingWeights, User, UserRole
from sqlalchemy import select

from sehaty.core.db.session import get_session
from sehaty.core.errors import SehatyForbiddenError, SehatyValidationError


@dataclass(frozen=True)
class RankingWeightsValues:
    """Detached, immutable snapshot of the ranking weights.

    Field names + defaults mirror the ``ranking_weights`` columns exactly, so a
    missing row yields the same values the DB would have defaulted to — search
    ranks identically whether or not an admin has ever written a row.
    """

    w_rating: float = 1.0
    w_distance: float = 1.0
    w_responsiveness: float = 0.5
    w_verified: float = 0.5
    w_recency: float = 0.25


# The tunable weight column names (single source of truth for validation).
_WEIGHT_FIELDS: frozenset[str] = frozenset(f.name for f in fields(RankingWeightsValues))


@dataclass(frozen=True)
class FeatureFlagValue:
    """Detached, immutable snapshot of a single feature flag."""

    key: str
    enabled: bool


class ConfigController:
    @staticmethod
    def get_ranking_weights() -> RankingWeightsValues:
        """Return the ranking-weights singleton, or defaults when unset.

        Reads the lowest-id ``ranking_weights`` row and projects it into an
        immutable snapshot. Never returns ``None``: with no row the dataclass
        defaults (identical to the ORM column defaults) are returned, keeping
        search ranking unchanged until an admin writes weights.
        """
        with get_session() as session:
            row = (
                session.execute(select(RankingWeights).order_by(RankingWeights.id))
                .scalars()
                .first()
            )
            if row is None:
                return RankingWeightsValues()
            return RankingWeightsValues(
                w_rating=row.w_rating,
                w_distance=row.w_distance,
                w_responsiveness=row.w_responsiveness,
                w_verified=row.w_verified,
                w_recency=row.w_recency,
            )

    @staticmethod
    def set_ranking_weights(admin_id: int, **weights: float) -> RankingWeightsValues:
        """Upsert the ranking-weights singleton (ADMIN only).

        Each keyword must name a known weight column and be a non-negative
        number; unknown keys or negative values raise ``SehatyValidationError``.
        Updates the existing singleton row in place (or creates the first one),
        leaving any unspecified weights untouched. Returns the updated snapshot.
        A non-admin actor raises ``SehatyForbiddenError``.
        """
        if not weights:
            raise SehatyValidationError("no ranking weights provided")
        for name, value in weights.items():
            if name not in _WEIGHT_FIELDS:
                raise SehatyValidationError(f"unknown ranking weight: {name!r}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SehatyValidationError(f"weight {name!r} must be a number, got {value!r}")
            if value < 0:
                raise SehatyValidationError(f"weight {name!r} must be non-negative, got {value}")

        with get_session() as session:
            ConfigController._require_admin(session, admin_id)
            row = (
                session.execute(select(RankingWeights).order_by(RankingWeights.id))
                .scalars()
                .first()
            )
            if row is None:
                row = RankingWeights()
                session.add(row)
            for name, value in weights.items():
                setattr(row, name, float(value))
            session.flush()
            return RankingWeightsValues(
                w_rating=row.w_rating,
                w_distance=row.w_distance,
                w_responsiveness=row.w_responsiveness,
                w_verified=row.w_verified,
                w_recency=row.w_recency,
            )

    @staticmethod
    def list_feature_flags() -> dict[str, bool]:
        """Return every feature flag as a ``{key: enabled}`` mapping."""
        with get_session() as session:
            rows = session.execute(select(FeatureFlag.key, FeatureFlag.enabled)).all()
        return {row.key: bool(row.enabled) for row in rows}

    @staticmethod
    def get_feature_flag(key: str, default: bool = False) -> bool:
        """Return a flag's ``enabled`` state, or ``default`` when it is unset."""
        with get_session() as session:
            enabled = session.execute(
                select(FeatureFlag.enabled).where(FeatureFlag.key == key)
            ).scalar_one_or_none()
        return default if enabled is None else bool(enabled)

    @staticmethod
    def set_feature_flag(admin_id: int, key: str, enabled: bool) -> FeatureFlagValue:
        """Upsert a named feature flag (ADMIN only); return the flag.

        Creates the flag on first sight or flips an existing one. A blank key
        raises ``SehatyValidationError``; a non-admin actor raises
        ``SehatyForbiddenError``.
        """
        if not key or not key.strip():
            raise SehatyValidationError("feature flag key is required")
        key = key.strip()
        with get_session() as session:
            ConfigController._require_admin(session, admin_id)
            flag = session.execute(
                select(FeatureFlag).where(FeatureFlag.key == key)
            ).scalar_one_or_none()
            if flag is None:
                flag = FeatureFlag(key=key, enabled=enabled)
                session.add(flag)
            else:
                flag.enabled = enabled
            session.flush()
            return FeatureFlagValue(key=flag.key, enabled=bool(flag.enabled))

    @staticmethod
    def _require_admin(session, admin_id: int) -> None:
        """Raise ``SehatyForbiddenError`` unless ``admin_id`` is an ADMIN user."""
        role = session.execute(select(User.role).where(User.id == admin_id)).scalar_one_or_none()
        if role != UserRole.ADMIN:
            raise SehatyForbiddenError("only an admin may change configuration")
