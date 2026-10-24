"""Shared test fixtures for the partner platform.

Provides an isolated in-memory SQLite DB per test (StaticPool → one shared
connection so background workers that open their own session see the same data)
and keeps the agent registry clean between tests.
"""
import os

# The Txn Egress listener binds a real TLS port at app startup; tests that spin up
# several app instances would collide on it.
os.environ.setdefault("TXN_EGRESS_AUTOSTART", "false")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
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
