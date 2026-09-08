# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""F-29 — readiness must not be declarable on milestones the authority never got.

`report_progress` reports a delivery failure by RETURNING None (the send is then
queued in OutboundA2ARetry); it does not raise. The local ProgressReport row is
written either way, and `submit_readiness` used to gate purely on those rows. So
three undelivered milestones still satisfied the gate and flipped the platform to
`ready` against an assignment the authority had recorded no progress on.

These tests pin both halves: the gate now consults the retry queue, and the
declaration itself is no longer allowed to fail open.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import app.database as database
from app.api.auth import get_current_user, require_admin
from app.database import get_db
from app.main import app
from app.models import (
    Base, IncomingChange, OutboundA2ARetry, PartnerUser, ProgressReport,
)

CHANGE_ID = "chg-local-1"
AUTHORITY_CHANGE_ID = "auth-chg-1"
ALL_STEPS = ["design_completed", "coding_completed", "testing_completed"]


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

    session = database.SessionLocal()
    session.add(IncomingChange(
        id=CHANGE_ID,
        authority_change_id=AUTHORITY_CHANGE_ID,
        title="Test change",
        status="received",
    ))
    # All three milestones reported LOCALLY — the pre-fix gate's only input.
    for step in ALL_STEPS:
        session.add(ProgressReport(change_id=CHANGE_ID, step=step))
    session.commit()
    session.close()

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)


def _queue_failed_milestone(status: str, milestone: str = "coding") -> None:
    """Simulate a milestone_update whose send came back None and was queued."""
    db = database.SessionLocal()
    db.add(OutboundA2ARetry(
        change_id=AUTHORITY_CHANGE_ID,
        task_type="milestone_update",
        payload={"milestone": milestone, "state": "completed"},
        status=status,
    ))
    db.commit()
    db.close()


def _patch_declare_ready(monkeypatch, result):
    monkeypatch.setattr(
        "app.api.dashboard.certification.declare_ready",
        lambda *a, **k: result,
    )


def test_readiness_blocked_while_milestone_still_queued(client, monkeypatch):
    """A pending retry means the authority has not seen the milestone YET."""
    _patch_declare_ready(monkeypatch, {"status": "delivered"})
    _queue_failed_milestone("pending")

    resp = client.post(f"/api/changes/{CHANGE_ID}/ready", json={"role": "PAYER_PSP"})

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "have not reached the authority" in detail
    assert "coding" in detail
    assert "still queued for retry" in detail


def test_readiness_blocked_when_milestone_abandoned(client, monkeypatch):
    """An abandoned retry will NEVER self-heal — the operator must re-drive it."""
    _patch_declare_ready(monkeypatch, {"status": "delivered"})
    _queue_failed_milestone("abandoned", milestone="testing")

    resp = client.post(f"/api/changes/{CHANGE_ID}/ready", json={"role": "PAYER_PSP"})

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "ABANDONED" in detail
    assert "testing" in detail


def test_readiness_not_blocked_by_an_already_delivered_retry(client, monkeypatch):
    """A row the sweeper delivered must NOT keep blocking — the gate has to
    self-correct, otherwise one transient failure bricks the change forever."""
    _patch_declare_ready(monkeypatch, {"status": "delivered"})
    _queue_failed_milestone("delivered")

    resp = client.post(f"/api/changes/{CHANGE_ID}/ready", json={"role": "PAYER_PSP"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["declared"] is True


def test_readiness_not_blocked_by_another_changes_failure(client, monkeypatch):
    """The gate must scope to THIS change, not any failed milestone anywhere."""
    _patch_declare_ready(monkeypatch, {"status": "delivered"})
    db = database.SessionLocal()
    db.add(OutboundA2ARetry(
        change_id="some-other-change",
        task_type="milestone_update",
        payload={"milestone": "design"},
        status="abandoned",
    ))
    db.commit()
    db.close()

    resp = client.post(f"/api/changes/{CHANGE_ID}/ready", json={"role": "PAYER_PSP"})

    assert resp.status_code == 200, resp.text


def test_readiness_not_blocked_by_a_different_task_type(client, monkeypatch):
    """Only milestone_update gates readiness; an unrelated failed send must not."""
    _patch_declare_ready(monkeypatch, {"status": "delivered"})
    db = database.SessionLocal()
    db.add(OutboundA2ARetry(
        change_id=AUTHORITY_CHANGE_ID,
        task_type="query",
        payload={"text": "unrelated"},
        status="abandoned",
    ))
    db.commit()
    db.close()

    resp = client.post(f"/api/changes/{CHANGE_ID}/ready", json={"role": "PAYER_PSP"})

    assert resp.status_code == 200, resp.text


def test_undelivered_declaration_does_not_flip_status_to_ready(client, monkeypatch):
    """declare_ready returning None is a delivery failure, not a success.

    This is the same fail-open one level up: the declaration is what unlocks
    certification on the authority side, so persisting `ready` without it leaves
    the two platforms disagreeing about whether certification may start.
    """
    _patch_declare_ready(monkeypatch, None)

    resp = client.post(f"/api/changes/{CHANGE_ID}/ready", json={"role": "PAYER_PSP"})

    assert resp.status_code == 502, resp.text
    assert "queued for retry" in resp.json()["detail"]

    db = database.SessionLocal()
    change = db.get(IncomingChange, CHANGE_ID)
    assert change.status != "ready", "status must not advance on an undelivered declaration"
    db.close()


def test_readiness_succeeds_when_everything_delivered(client, monkeypatch):
    """The happy path still works — the guard is not blanket-refusing."""
    _patch_declare_ready(monkeypatch, {"status": "delivered"})

    resp = client.post(f"/api/changes/{CHANGE_ID}/ready", json={"role": "PAYER_PSP"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["declared"] is True
    assert body["sent_to_npci"] is True

    db = database.SessionLocal()
    assert db.get(IncomingChange, CHANGE_ID).status == "ready"
    db.close()
