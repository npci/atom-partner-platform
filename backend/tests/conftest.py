# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared test fixtures for the partner platform.

Provides an isolated in-memory SQLite DB per test (StaticPool → one shared
connection so background workers that open their own session see the same data)
and keeps the agent registry clean between tests.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import app.database as database
from app.agents import registry
from app.models import Base


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
