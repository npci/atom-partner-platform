# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Regression tests for the 04-09-2026 SAST report (5 findings, all confirmed).

Each finding gets a test that fails against the pre-fix tree, because the whole
class of bug here is "a guard exists but is not wired to the path that needs
it" — which no amount of asserting that the guard works would have caught.
`test_npci_ssrf_guard.py` already proved `_is_private_url()` was correct, and it
was; it was simply never called on the send path. So these assert REACHABILITY
of the guard from the real entry points, not the guard's own logic.

  F-001  npci_client: `_is_private_url` wired only into the Settings
         "Test Connection" probe, not into authenticate()/_dispatch_wire() —
         i.e. every production send.
  F-002  cert_trigger_url: validated for scheme/netloc but not destination, at
         either save time or call time, while being auto-dispatched to with a
         bearer token on every inbound cert_execution_start.
  F-003  POST /api/feasibility/analyse/{change_id}  — unauthenticated (LLM spend)
  F-004  GET  /api/feasibility/report/{change_id}   — unauthenticated (data)
  F-005  GET  /api/feasibility/profile/status       — unauthenticated (metadata)
"""
import secrets

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from app import database
from app.database import get_db
from app.main import app
from app.models import Base, PartnerUser


@pytest.fixture()
def ssrf_settings(monkeypatch):
    """Shipped defaults: nothing approved into private space.

    The guard reads settings at call time, so patching the live object is
    enough — matches the fixture in test_npci_ssrf_guard.py.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "npci_ssrf_allowed_hosts", "", raising=False)
    monkeypatch.setattr(settings, "npci_ssrf_allow_private_networks", False, raising=False)
    return settings


# ── F-001 — the A2A send path is guarded, not just the probe ────────────────


class TestOutboundA2AGuarded:
    def test_authenticate_refuses_link_local(self, ssrf_settings, monkeypatch):
        """The metadata-service address must not be dialled by authenticate().

        Pre-fix this returned None via the generic `except Exception` path only
        if the connection itself failed — it would have CONNECTED first.
        """
        import app.npci_client as nc

        monkeypatch.setattr(
            nc, "_get_a2a_base_url", lambda db: "http://169.254.169.254"
        )
        with pytest.raises(nc.OutboundURLBlocked):
            nc.authenticate(db=None)

    def test_authenticate_allows_public_host(self, ssrf_settings, monkeypatch):
        """The guard must not become a blanket refusal — a public host still
        proceeds (and then fails on the absent API key, which is the NEXT
        check, proving we got past the guard rather than short-circuiting)."""
        import app.npci_client as nc

        monkeypatch.setattr(nc, "_get_a2a_base_url", lambda db: "https://example.com")
        monkeypatch.setattr(nc, "_get_api_key", lambda db: "")
        assert nc.authenticate(db=None) is None

    def test_compose_service_name_still_allowed(self, ssrf_settings, monkeypatch):
        """The default deployment shape (`http://npci_backend:8000`) must keep
        working — the guard's `_backend` escape hatch is load-bearing here, and
        wiring the guard in must not break every existing compose install."""
        import app.npci_client as nc

        monkeypatch.setattr(nc, "_get_a2a_base_url", lambda db: "http://npci_backend:8000")
        monkeypatch.setattr(nc, "_get_api_key", lambda db: "")
        assert nc.authenticate(db=None) is None  # reached the api-key check

    def test_blocked_send_is_queued_not_connected(self, ssrf_settings, monkeypatch):
        """A refused URL must fail the send the way a transport error does —
        queued for retry, returns None — rather than raising past send_task()
        into ~30 call sites that only handle a falsy return."""
        import app.npci_client as nc

        monkeypatch.setattr(nc, "_get_a2a_base_url", lambda db: "http://169.254.169.254")
        queued = []
        monkeypatch.setattr(
            nc, "_maybe_enqueue_retry",
            lambda db, cid, tt, pl, err, corr, idem=None: queued.append((tt, err)),
        )
        assert nc.send_task(None, "echo", None, {}) is None
        assert queued and queued[0][0] == "echo"
        # The retry row carries the exception TYPE (CWE-209 — never the host).
        assert "OutboundURLBlocked" in queued[0][1]


# ── F-002 — cert trigger guarded at save time AND call time ─────────────────


class TestCertTriggerGuarded:
    def test_fire_trigger_refuses_private_url(self, ssrf_settings):
        """Call-time guard: covers rows written before the save-time check
        existed, which is the realistic exposure — the setting is already
        stored in deployed instances."""
        from app.services.integration_testing.trigger import fire_trigger

        # Generated, not a literal — repo convention for test credentials
        # (see test_no_hardcoded_secret_literals.py; a fixed string would both
        # trip that gate and weaken the case).
        assert fire_trigger(
            "http://169.254.169.254/latest/meta-data/",
            secrets.token_urlsafe(32),
            test_case_id="TC-1",
            cert_context={},
            case_data=None,
            reply_via="a2a://sim",
        ) is False

    def test_fire_trigger_sends_no_request_when_blocked(self, ssrf_settings, monkeypatch):
        """Blocked must mean NOT DIALLED. Returning False after leaking the
        bearer token to the metadata service would satisfy the test above
        while missing the entire point of the finding."""
        import httpx

        from app.services.integration_testing.trigger import fire_trigger

        def _boom(*a, **k):
            raise AssertionError("SSRF guard let a request through to a blocked host")

        monkeypatch.setattr(httpx.Client, "post", _boom)
        assert fire_trigger(
            "http://10.0.0.1/trigger", secrets.token_urlsafe(32),
            test_case_id="TC-1", cert_context={}, case_data=None, reply_via="a2a://sim",
        ) is False


# ── F-003 / F-004 / F-005 — feasibility router behind auth ─────────────────


@pytest.fixture()
def anon_client():
    """A client with NO auth override — an anonymous caller off the internet."""
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

    app.dependency_overrides[get_db] = _override_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)


class TestFeasibilityRequiresAuth:
    @pytest.mark.parametrize(
        ("method", "path", "finding"),
        [
            ("post", "/api/feasibility/analyse/c-1", "F-003"),
            ("get", "/api/feasibility/report/c-1", "F-004"),
            ("get", "/api/feasibility/profile/status", "F-005"),
        ],
    )
    def test_anonymous_is_rejected(self, anon_client, method, path, finding):
        resp = getattr(anon_client, method)(path)
        assert resp.status_code in (401, 403), (
            f"{finding}: {method.upper()} {path} answered {resp.status_code} to an "
            "unauthenticated caller"
        )

    def test_authenticated_user_still_served(self, anon_client):
        """The boundary must admit a logged-in user — a 401 for everyone would
        pass the tests above while breaking the feature."""
        from app.api.auth import get_current_user

        app.dependency_overrides[get_current_user] = lambda: PartnerUser(
            id="u-1", username="u", password_hash="x",
            full_name="U", role="admin", is_active=True,
        )
        resp = anon_client.get("/api/feasibility/profile/status")
        assert resp.status_code == 200

    def test_router_level_covers_future_routes(self):
        """Assert the dependency sits on the ROUTER. A per-route spelling is
        what allowed the original gap, so re-introducing it is the regression
        worth catching — a new route added here must be closed by default.
        """
        from app.api.auth import get_current_user
        from app.api.feasibility import router

        deps = [d.dependency for d in router.dependencies]
        assert get_current_user in deps
