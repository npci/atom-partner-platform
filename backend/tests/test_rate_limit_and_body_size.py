# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the A2A rate limiter (Finding 10) and the global body-size
backstop (Finding 9's defense-in-depth outside the A2A mount).
"""
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.a2a_common.rate_limit_middleware import A2ARateLimitMiddleware, _FixedWindow
from app.core.body_size_middleware import MaxBodySizeMiddleware


async def _ok(request):
    return JSONResponse({"ok": True})


class TestFixedWindow:
    def test_allows_up_to_limit(self):
        w = _FixedWindow(limit=3, window_s=10)
        assert w.allow()[0] is True
        assert w.allow()[0] is True
        assert w.allow()[0] is True

    def test_rejects_beyond_limit_within_window(self):
        w = _FixedWindow(limit=2, window_s=10)
        w.allow()
        w.allow()
        allowed, retry_after = w.allow()
        assert allowed is False
        assert retry_after >= 0

    def test_resets_after_window_elapses(self):
        import time
        w = _FixedWindow(limit=1, window_s=0.05)
        assert w.allow()[0] is True
        assert w.allow()[0] is False
        time.sleep(0.06)
        assert w.allow()[0] is True


class TestA2ARateLimitMiddleware:
    def _client(self, limit_rps: int) -> TestClient:
        app = Starlette(routes=[Route("/rpc", _ok, methods=["POST"])])
        app.add_middleware(A2ARateLimitMiddleware, limit_rps=limit_rps)
        return TestClient(app)

    def test_requests_within_limit_pass(self):
        client = self._client(limit_rps=5)
        for _ in range(5):
            resp = client.post("/rpc")
            assert resp.status_code == 200

    def test_requests_beyond_limit_are_throttled(self):
        client = self._client(limit_rps=2)
        client.post("/rpc")
        client.post("/rpc")
        resp = client.post("/rpc")
        assert resp.status_code == 429
        assert resp.json()["error"] == "rate_limited"
        assert "retry-after" in resp.headers


class TestMaxBodySizeMiddleware:
    def _client(self, max_bytes: int) -> TestClient:
        app = Starlette(routes=[Route("/x", _ok, methods=["POST"])])
        app.add_middleware(MaxBodySizeMiddleware, max_bytes=max_bytes)
        return TestClient(app)

    def test_small_body_passes(self):
        client = self._client(max_bytes=1000)
        resp = client.post("/x", content=b"small")
        assert resp.status_code == 200

    def test_oversized_content_length_rejected_with_413(self):
        client = self._client(max_bytes=10)
        resp = client.post("/x", content=b"x" * 1000)
        assert resp.status_code == 413

    def test_no_content_length_passes_through(self):
        # Chunked/streaming requests without Content-Length are not blocked by
        # this backstop (by design — see module docstring); the A2A mount's
        # own streaming-aware guard covers that path specifically.
        client = self._client(max_bytes=10)
        resp = client.post("/x")  # empty body, no explicit content
        assert resp.status_code == 200


class _FakeRedis:
    """Minimal INCR/EXPIRE stand-in. Shared between middleware instances in a
    test to represent what real redis represents: one counter, many workers."""

    def __init__(self, fail_after: int | None = None):
        self.store: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self._calls = 0
        self._fail_after = fail_after

    def incr(self, key: str) -> int:
        self._calls += 1
        if self._fail_after is not None and self._calls > self._fail_after:
            raise ConnectionError("redis is down")
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key: str, ttl: int) -> None:
        self.expiries[key] = ttl


class TestRedisFixedWindow:
    """UPP-04 / AR-11: the window must be SHARED, so that N workers enforce one
    limit rather than N."""

    def _mw(self, client, limit_rps: int):
        app = Starlette(routes=[Route("/rpc", _ok, methods=["POST"])])
        app.add_middleware(A2ARateLimitMiddleware, limit_rps=limit_rps,
                           redis_url="redis://x", redis_client=client)
        return TestClient(app)

    def test_one_shared_window_across_two_processes(self):
        # Two middleware instances = two worker processes against one redis.
        shared = _FakeRedis()
        w1, w2 = self._mw(shared, 2), self._mw(shared, 2)
        assert w1.post("/rpc").status_code == 200      # 1
        assert w2.post("/rpc").status_code == 200      # 2 — other worker
        # The third request exceeds the limit even though each worker has only
        # served one or two. Per-process windows would have allowed it.
        assert w2.post("/rpc").status_code == 429

    def test_per_process_windows_would_not_have_caught_it(self):
        # Same traffic, no shared backend: both workers allow everything,
        # which is exactly the finding.
        app_a = Starlette(routes=[Route("/rpc", _ok, methods=["POST"])])
        app_a.add_middleware(A2ARateLimitMiddleware, limit_rps=2, redis_url="")
        app_b = Starlette(routes=[Route("/rpc", _ok, methods=["POST"])])
        app_b.add_middleware(A2ARateLimitMiddleware, limit_rps=2, redis_url="")
        a, b = TestClient(app_a), TestClient(app_b)
        assert a.post("/rpc").status_code == 200
        assert b.post("/rpc").status_code == 200
        assert b.post("/rpc").status_code == 200   # 3rd overall, still allowed

    def test_sets_a_ttl_so_buckets_cannot_leak(self):
        shared = _FakeRedis()
        self._mw(shared, 5).post("/rpc")
        assert shared.expiries, "first INCR in a bucket must set an expiry"
        assert all(ttl >= 2 for ttl in shared.expiries.values())

    def test_redis_outage_degrades_to_per_process_not_to_nothing(self):
        # Fails from the second call onward; the limiter must keep enforcing
        # via the in-process fallback rather than letting traffic through.
        shared = _FakeRedis(fail_after=1)
        c = self._mw(shared, 1)
        assert c.post("/rpc").status_code == 200    # served by redis
        # redis now raises; fallback window has limit=1 and is already at 0,
        # so this is allowed, and the one after it is refused.
        c.post("/rpc")
        assert c.post("/rpc").status_code == 429

    def test_falls_back_when_no_url_configured(self):
        app = Starlette(routes=[Route("/rpc", _ok, methods=["POST"])])
        mw = A2ARateLimitMiddleware(app, limit_rps=5, redis_url="")
        assert mw.shared is False

    def test_reports_shared_when_a_client_is_present(self):
        app = Starlette(routes=[Route("/rpc", _ok, methods=["POST"])])
        mw = A2ARateLimitMiddleware(app, limit_rps=5, redis_url="redis://x",
                                    redis_client=_FakeRedis())
        assert mw.shared is True
