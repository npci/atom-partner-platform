# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Controls that only existed at the nginx edge, and one that existed nowhere.

The backend is not always behind `deploy/edge.nginx.conf`:
`docker-compose.override.yml` publishes it on 127.0.0.1:18001, and a host
install may front it with something else. On those paths the API answered with
HSTS and nothing else — no nosniff, no framing defence, no Referrer-Policy.

The throttle is the sharper gap: `A2ARateLimitMiddleware` is installed on the
/a2a-rpc SUB-APP by mount.py, and Starlette middleware on a mount does not
apply to the parent, so no dashboard or auth route was rate limited at all.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


class TestSecurityHeaders:
    @pytest.mark.parametrize("header,expected", [
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        ("content-security-policy", "frame-ancestors 'none'"),
        ("referrer-policy", "strict-origin-when-cross-origin"),
    ])
    def test_present_on_an_api_response(self, client, header, expected):
        r = client.get("/api/health")
        assert r.headers.get(header) == expected

    def test_hsts_still_present(self, client):
        """The header this middleware originally shipped for."""
        r = client.get("/api/health")
        assert "max-age=" in r.headers.get("strict-transport-security", "")

    def test_headers_are_not_duplicated(self, client):
        """The edge sets these too. `add_header` in nginx APPENDS rather than
        replaces, and duplicated CSPs combine conjunctively — so emitting a
        second one unconditionally could tighten a working policy into a
        broken one. The middleware only fills in what is absent."""
        r = client.get("/api/health")
        raw = r.headers.raw
        for name in (b"content-security-policy", b"x-frame-options",
                     b"strict-transport-security"):
            count = sum(1 for k, _ in raw if k.lower() == name)
            assert count <= 1, f"{name!r} emitted {count} times"


class TestDashboardThrottle:
    def test_probe_endpoints_are_exempt(self, client):
        """Throttling liveness turns a busy moment into a restart loop."""
        for _ in range(50):
            assert client.get("/api/health").status_code == 200

    def test_limit_is_enforced_per_caller(self, monkeypatch):
        """Distinct sessions get distinct buckets — one operator's bulk
        activity must not 429 anybody else. A GLOBAL window would be the
        shared-bucket mistake the login lockout already made."""
        from app.core.dashboard_rate_limit import _KeyedFixedWindow

        w = _KeyedFixedWindow(limit=3, window_s=60.0)
        for _ in range(3):
            assert w.allow("s:aaa")[0] is True
        assert w.allow("s:aaa")[0] is False        # first caller exhausted
        assert w.allow("s:bbb")[0] is True         # second caller unaffected

    def test_window_resets(self):
        from app.core.dashboard_rate_limit import _KeyedFixedWindow

        w = _KeyedFixedWindow(limit=1, window_s=0.05)
        assert w.allow("k")[0] is True
        assert w.allow("k")[0] is False
        import time
        time.sleep(0.06)
        assert w.allow("k")[0] is True

    def test_bucket_table_is_bounded(self):
        """An attacker rotating the key must not grow the table without limit."""
        from app.core.dashboard_rate_limit import _KeyedFixedWindow

        w = _KeyedFixedWindow(limit=1, window_s=0.01)
        for i in range(5000):
            w.allow(f"k{i}")
        assert len(w._buckets) <= 4096 + 1

    def test_cookie_name_matches_the_auth_module(self):
        """A rename in api.auth must not silently downgrade every browser
        request to the shared per-IP bucket."""
        from app.api.auth import COOKIE_NAME
        from app.core.dashboard_rate_limit import DashboardRateLimitMiddleware

        mw = DashboardRateLimitMiddleware(lambda *a: None, limit=1)
        assert mw._cookie_name == COOKIE_NAME
