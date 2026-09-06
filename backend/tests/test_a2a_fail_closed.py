# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the fail-closed A2A ingress default
(docs/adr/ADR-0003-fail-closed-a2a-ingress.md) and the inbound body-size limit
(Finding 9: security_architecture_skills.md §4.1/§11.1).

Uses a minimal Starlette app wrapping just the middleware under test, driven
via Starlette's synchronous TestClient — no real network, no real A2A SDK
routing, and no new async-test dependency (this suite has none today; see
requirements.txt's own note on why pytest-asyncio was deliberately omitted).
"""
import base64
import json
import secrets as _secrets

import jwt
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.a2a_common.auth_middleware import PartnerAuthMiddleware
from app.a2a_common.hmac_middleware import PartnerHmacMiddleware
from app.a2a_common.hmac_signer import sign as hmac_sign
from app.config import settings
from app.core.secret_box import encrypt
from app.core.setting_keys import SettingKey


def _fresh_secret(label: str) -> str:
    """A random, single-use secret for one assertion.

    These tests need a shared secret on both sides of a signature or JWT
    check. They used to spell one out — `secret = "shared-hmac-secret"` — which
    Checkmarx's "Use Of Hardcoded Password" query reported as an embedded
    credential (paths 3 and 5). No real credential was ever involved, but the
    finding returns on every rescan, so the literal is gone.

    Generating the value is also the better test. A fixed string can mask a bug
    that a constant happens to satisfy; a fresh 32-byte secret per call proves
    the middleware round-trips whatever it is actually given. `label` is woven
    in purely so a failure message says which secret was in play.
    """
    return f"{label}-{_secrets.token_urlsafe(32)}"


async def _echo(request):
    body = await request.body()
    return JSONResponse({"ok": True, "len": len(body)})


def _hmac_app(*, max_body_bytes: int | None = None):
    app = Starlette(routes=[Route("/rpc", _echo, methods=["POST"])])
    app.add_middleware(PartnerHmacMiddleware, max_body_bytes=max_body_bytes)
    return TestClient(app)


def _auth_app():
    app = Starlette(routes=[Route("/rpc", _echo, methods=["POST"])])
    app.add_middleware(PartnerAuthMiddleware)
    return TestClient(app)


def _set_setting(db_session, key: str, value: str):
    from app.models import PartnerSetting
    db_session.add(PartnerSetting(key=key, value=encrypt(value)))
    db_session.commit()


@pytest.fixture(autouse=True)
def _kek(monkeypatch):
    key = base64.b64encode(_secrets.token_bytes(32)).decode()
    monkeypatch.setenv("PARTNER_SECRET_KEK", key)
    monkeypatch.setattr(settings, "partner_allow_unauthenticated_a2a", False)
    yield


class TestHmacMiddlewareFailClosed:
    def test_rejects_with_503_when_secret_unconfigured(self, db_session):
        client = _hmac_app(max_body_bytes=1_000_000)
        resp = client.post("/rpc", json={"x": 1})
        assert resp.status_code == 503
        assert resp.json()["error"] == "envelope_not_configured"

    def test_bypass_escape_hatch_allows_when_explicitly_set(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "partner_allow_unauthenticated_a2a", True)
        client = _hmac_app(max_body_bytes=1_000_000)
        resp = client.post("/rpc", json={"x": 1})
        assert resp.status_code == 200

    def test_valid_signature_passes(self, db_session):
        secret = _fresh_secret("hmac")
        _set_setting(db_session, SettingKey.npci_hmac_secret, secret)
        client = _hmac_app(max_body_bytes=1_000_000)
        body = json.dumps({"x": 1}).encode()
        envelope = hmac_sign(body, secret)
        resp = client.post(
            "/rpc", content=body,
            headers={"content-type": "application/json", **envelope},
        )
        assert resp.status_code == 200
        assert resp.json()["len"] == len(body)

    def test_invalid_signature_rejected(self, db_session):
        _set_setting(db_session, SettingKey.npci_hmac_secret, _fresh_secret("hmac"))
        client = _hmac_app(max_body_bytes=1_000_000)
        body = json.dumps({"x": 1}).encode()
        # Signed with a DIFFERENT secret than the one installed above.
        bad_envelope = hmac_sign(body, _fresh_secret("wrong"))
        resp = client.post(
            "/rpc", content=body,
            headers={"content-type": "application/json", **bad_envelope},
        )
        assert resp.status_code == 401
        assert resp.json()["error"] == "signature_mismatch"


class TestHmacMiddlewareBodySizeLimit:
    def test_oversized_body_rejected_with_413(self, db_session):
        _set_setting(db_session, SettingKey.npci_hmac_secret, _fresh_secret("hmac"))
        client = _hmac_app(max_body_bytes=100)  # tiny limit for the test
        oversized_body = b"x" * 1000
        resp = client.post(
            "/rpc", content=oversized_body, headers={"content-type": "application/json"},
        )
        assert resp.status_code == 413
        assert resp.json()["error"] == "payload_too_large"

    def test_body_within_limit_reaches_hmac_check(self, db_session):
        # No secret configured -> 503 (fail-closed), NOT 413 — proves the body
        # was accepted by the size guard and the request proceeded past it.
        client = _hmac_app(max_body_bytes=100)
        small_body = b"x" * 50
        resp = client.post(
            "/rpc", content=small_body, headers={"content-type": "application/json"},
        )
        assert resp.status_code == 503  # not 413


class TestAuthMiddlewareFailClosed:
    def test_rejects_with_503_when_secret_unconfigured(self, db_session):
        client = _auth_app()
        resp = client.post("/rpc", json={"x": 1})
        assert resp.status_code == 503
        assert resp.json()["error"] == "jwt_not_configured"

    def test_bypass_escape_hatch_allows_when_explicitly_set(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "partner_allow_unauthenticated_a2a", True)
        client = _auth_app()
        resp = client.post("/rpc", json={"x": 1})
        assert resp.status_code == 200

    def test_valid_jwt_passes(self, db_session):
        secret = _fresh_secret("jwt")
        _set_setting(db_session, SettingKey.npci_jwt_secret, secret)
        token = jwt.encode({"type": "a2a", "sub": "npci"}, secret, algorithm="HS256")
        client = _auth_app()
        resp = client.post("/rpc", json={"x": 1}, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    def test_invalid_jwt_rejected(self, db_session):
        _set_setting(db_session, SettingKey.npci_jwt_secret, _fresh_secret("jwt"))
        client = _auth_app()
        resp = client.post("/rpc", json={"x": 1}, headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401
