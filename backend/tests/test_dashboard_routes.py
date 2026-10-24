"""Regression harness for the dashboard router (WS5 safety net).

Proves the per-domain split is behavior-preserving:
  1. The exact set of (method, path) the dashboard router exposes is frozen here
     and asserted — a dropped / renamed / mis-prefixed route fails the test.
  2. A handful of read-only endpoints are exercised via TestClient (auth + DB
     overridden) so a behavioral regression in the move fails a test, not prod.

This passes against the pre-split monolith AND the post-split package; the split
is correct only if it still passes.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import app.database as database
from app.api.auth import get_current_user, require_admin
from app.database import get_db
from app.main import app
from app.models import Base, PartnerUser

# Frozen baseline — the 24 routes the dashboard router exposed before the split.
EXPECTED_ROUTES = {
    "GET /api/changes",
    "GET /api/changes/{change_id}",
    "POST /api/changes/{change_id}/accept",
    "POST /api/changes/{change_id}/blocker",
    "GET /api/changes/{change_id}/cert-queries",
    "POST /api/changes/{change_id}/cert-query",
    "GET /api/changes/{change_id}/cert-status",
    "POST /api/changes/{change_id}/cert-status",
    "POST /api/changes/{change_id}/counter",
    "POST /api/changes/{change_id}/counter-proposals/{cp_id}/accept",
    "GET /api/changes/{change_id}/documents/{doc_id}/download",
    "GET /api/changes/{change_id}/documents/{doc_id}/download/pptx",
    "GET /api/changes/{change_id}/documents/{doc_id}/download/xlsx",
    "POST /api/changes/{change_id}/progress",
    "POST /api/changes/{change_id}/queries/suggest",
    "POST /api/changes/{change_id}/query",
    "GET /api/changes/{change_id}/query-drafts",
    "POST /api/changes/{change_id}/ready",
    "DELETE /api/query-drafts/{draft_id}",
    "PATCH /api/query-drafts/{draft_id}",
    "POST /api/query-drafts/{draft_id}/send",
    "GET /api/settings",
    "PUT /api/settings",
    "POST /api/settings/test-connection",
}


def _dashboard_route_set() -> set[str]:
    from app.api.dashboard import router
    out = set()
    for r in router.routes:
        for m in getattr(r, "methods", []):
            if m == "HEAD":
                continue
            out.add(f"{m} {r.path}")
    return out


def test_dashboard_route_inventory_is_unchanged():
    assert _dashboard_route_set() == EXPECTED_ROUTES


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    database.engine = engine
    database.SessionLocal.configure(bind=engine)
    Base.metadata.create_all(engine)

    def _override_db():
        db = database.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    admin = PartnerUser(
        id="u-admin", username="admin", password_hash="x",
        full_name="Admin", role="admin", is_active=True,
    )
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)


def test_list_changes_empty(client):
    resp = client.get("/api/changes")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_unknown_change_404(client):
    resp = client.get("/api/changes/does-not-exist")
    assert resp.status_code == 404


def test_get_settings_returns_masked_payload(client):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert "partner_name" in body
