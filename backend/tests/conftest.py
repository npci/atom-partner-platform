# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared test fixtures for the partner platform.

Provides an isolated in-memory SQLite DB per test (StaticPool → one shared
connection so background workers that open their own session see the same data)
and keeps the agent registry clean between tests.
"""
import os
import secrets

# MUST run before the `app.*` imports below.
#
# `app.api.auth` reads the login-session signing key ONCE, at import time, into
# the module constant `JWT_SECRET`. `config.py` deliberately defaults it to ""
# so the production boot guard can refuse an unset secret — which means that
# with no `SESSION_JWT_SECRET` exported, every test that mints or verifies a
# session token dies on `InvalidKeyError: HMAC key must not be empty`. That
# reads as a broken auth implementation; it is really an unset variable, and it
# cannot be repaired from inside a test because the constant is already bound
# by the time any test body runs.
#
# `setdefault`, so a caller exporting their own value still wins. High-entropy
# hex rather than something readable because `core/key_strength` rejects
# placeholder vocabulary ('secret', 'password', 'test', ...) — the same
# reasoning is spelled out in test_config_boot_guards.py — and 32 bytes so
# PyJWT does not warn about HS256 key length.
#
# GENERATED, not a literal. A hard-coded high-entropy hex string is
# indistinguishable from a real leaked key to a scanner reading the file, and
# gitleaks flagged this line as `generic-api-key` — a red gate on a value that
# was never a secret. Minting it per run removes the finding without
# allowlisting anything, so the scanner keeps its teeth. `token_hex(16)` is
# exactly the format `core/key_strength` documents as legitimate, and its
# thresholds were calibrated against it, so this cannot flake.
os.environ.setdefault("SESSION_JWT_SECRET", secrets.token_hex(16))

import pytest  # noqa: E402 — must follow the env default above
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.database as database  # noqa: E402
from app.agents import registry  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    database.engine = engine
    database.SessionLocal.configure(bind=engine)
    Base.metadata.create_all(engine)
    session = database.SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Force a clean registry rebuild around each test so one test's manifest
    override (e.g. the mcp: reserved test) can't leak into the next."""
    registry.clear()
    yield
    registry.clear()
