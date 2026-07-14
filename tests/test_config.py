"""Admin-config core tests on an in-memory SQLite engine.

Covers the ranking-weights singleton (defaults when unset, admin upsert,
non-admin + negative-weight guards), the feature-flag toggles, and the pure
``score_doctor`` ranking helper (two weight configs producing opposite
orderings). None of the touched tables carry the PostGIS ``geopoint`` column,
so no dialect shims are needed and the geo search stays in the PostGIS-gated
``test_doctor_search`` suite.
"""

import pytest
from sehaty.db import (
    AdminConfig,
    FeatureFlag,
    RankingWeights,
    User,
    UserRole,
)
from sehaty.db.base import SehatyBase
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sehaty.core.controllers.config import (
    ConfigController,
    FeatureFlagValue,
    RankingWeightsValues,
)
from sehaty.core.controllers.doctor_search import score_doctor
from sehaty.core.db import session as session_mod
from sehaty.core.errors import SehatyForbiddenError, SehatyValidationError

_TABLES = [
    User.__table__,
    RankingWeights.__table__,
    FeatureFlag.__table__,
    AdminConfig.__table__,
]


@pytest.fixture
def db() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SehatyBase.metadata.create_all(engine, tables=_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session_mod.set_session_factory(factory)
    yield factory
    session_mod.set_session_factory(None)


def _seed_user(factory: sessionmaker[Session], *, email: str, role: UserRole) -> int:
    with factory() as s:
        user = User(email=email, role=role, is_active=True)
        s.add(user)
        s.commit()
        return user.id


def _seed_admin(factory: sessionmaker[Session], email: str = "admin@sehaty.ma") -> int:
    return _seed_user(factory, email=email, role=UserRole.ADMIN)


def _seed_doctor(factory: sessionmaker[Session], email: str = "doc@clinic.ma") -> int:
    return _seed_user(factory, email=email, role=UserRole.DOCTOR)


# --------------------------------------------------------------------------- #
# Ranking weights
# --------------------------------------------------------------------------- #


def test_get_ranking_weights_returns_defaults_when_empty(db: sessionmaker[Session]) -> None:
    weights = ConfigController.get_ranking_weights()
    # Defaults mirror the ranking_weights column defaults exactly, so search
    # ranking is unchanged until an admin writes a row.
    assert weights == RankingWeightsValues(
        w_rating=1.0,
        w_distance=1.0,
        w_responsiveness=0.5,
        w_verified=0.5,
        w_recency=0.25,
    )


def test_set_ranking_weights_persists_and_get_returns_them(db: sessionmaker[Session]) -> None:
    admin = _seed_admin(db)

    updated = ConfigController.set_ranking_weights(
        admin, w_rating=3.0, w_distance=0.5, w_verified=0.0
    )
    assert updated.w_rating == 3.0
    assert updated.w_distance == 0.5
    assert updated.w_verified == 0.0
    # Unspecified weights keep their defaults.
    assert updated.w_responsiveness == 0.5
    assert updated.w_recency == 0.25

    fetched = ConfigController.get_ranking_weights()
    assert fetched == updated

    # Upsert stays a singleton: a second write updates the same row.
    ConfigController.set_ranking_weights(admin, w_rating=7.0)
    with db() as s:
        rows = s.execute(select(RankingWeights)).scalars().all()
    assert len(rows) == 1
    assert ConfigController.get_ranking_weights().w_rating == 7.0
    # The earlier w_distance override survives the partial update.
    assert ConfigController.get_ranking_weights().w_distance == 0.5


def test_set_ranking_weights_non_admin_forbidden(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyForbiddenError):
        ConfigController.set_ranking_weights(doc, w_rating=2.0)
    # Nothing was written.
    with db() as s:
        assert s.execute(select(RankingWeights)).scalars().first() is None


def test_set_ranking_weights_negative_is_validation_error(db: sessionmaker[Session]) -> None:
    admin = _seed_admin(db)
    with pytest.raises(SehatyValidationError):
        ConfigController.set_ranking_weights(admin, w_rating=-1.0)


def test_set_ranking_weights_unknown_field_is_validation_error(db: sessionmaker[Session]) -> None:
    admin = _seed_admin(db)
    with pytest.raises(SehatyValidationError):
        ConfigController.set_ranking_weights(admin, w_bogus=1.0)


# --------------------------------------------------------------------------- #
# Feature flags
# --------------------------------------------------------------------------- #


def test_get_feature_flag_defaults_false(db: sessionmaker[Session]) -> None:
    assert ConfigController.get_feature_flag("online_booking") is False
    assert ConfigController.get_feature_flag("online_booking", default=True) is True


def test_set_feature_flag_admin_then_get_and_list(db: sessionmaker[Session]) -> None:
    admin = _seed_admin(db)

    flag = ConfigController.set_feature_flag(admin, "online_booking", True)
    assert flag == FeatureFlagValue(key="online_booking", enabled=True)
    assert ConfigController.get_feature_flag("online_booking") is True
    assert ConfigController.list_feature_flags() == {"online_booking": True}

    # Flipping the same flag off updates in place (still one row).
    ConfigController.set_feature_flag(admin, "online_booking", False)
    assert ConfigController.get_feature_flag("online_booking") is False
    assert ConfigController.list_feature_flags() == {"online_booking": False}


def test_set_feature_flag_non_admin_forbidden(db: sessionmaker[Session]) -> None:
    doc = _seed_doctor(db)
    with pytest.raises(SehatyForbiddenError):
        ConfigController.set_feature_flag(doc, "online_booking", True)
    assert ConfigController.list_feature_flags() == {}


# --------------------------------------------------------------------------- #
# Pure ranking helper (no geo)
# --------------------------------------------------------------------------- #


def test_score_doctor_uses_configured_weights_to_reorder() -> None:
    """Two doctors, two weight configs, opposite orderings — no PostGIS needed.

    A near, poorly rated doctor vs a far, 5-star one. Balanced default weights
    keep the near doctor on top; cranking ``w_rating`` flips the far one above.
    """
    radius_m = 10_000.0
    near_low = {"avg_stars": 2.0, "distance_m": 500.0}
    far_high = {"avg_stars": 5.0, "distance_m": 8_000.0}

    balanced = RankingWeightsValues()  # defaults
    assert score_doctor(radius_m=radius_m, weights=balanced, **near_low) > score_doctor(
        radius_m=radius_m, weights=balanced, **far_high
    )

    rating_heavy = RankingWeightsValues(w_rating=10.0, w_distance=1.0, w_verified=0.5)
    assert score_doctor(radius_m=radius_m, weights=rating_heavy, **far_high) > score_doctor(
        radius_m=radius_m, weights=rating_heavy, **near_low
    )
