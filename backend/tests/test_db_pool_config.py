# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the externalized DB connection pool configuration (Finding 6)."""
from app.config import settings
from app.database import engine


def test_pool_size_matches_settings():
    assert engine.pool.size() == settings.db_pool_size


def test_pool_recycle_is_configured():
    # SQLAlchemy stores this on the pool as `_recycle`.
    assert engine.pool._recycle == settings.db_pool_recycle_s


def test_pool_timeout_is_configured():
    assert engine.pool._timeout == settings.db_pool_timeout_s


def test_defaults_are_sane_non_zero_values():
    assert settings.db_pool_size > 0
    assert settings.db_max_overflow >= 0
    assert settings.db_pool_timeout_s > 0
    assert settings.db_pool_recycle_s > 0
