# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-caller rate limit for the dashboard REST surface.

WHY THIS EXISTS SEPARATELY FROM THE A2A LIMITER
`a2a_common.rate_limit_middleware.A2ARateLimitMiddleware` is installed by
`mount.py` on the /a2a-rpc SUB-APP. Starlette middleware on a mounted sub-app
does not apply to the parent, so every dashboard and auth route — including
job creation, indexing and the LLM-backed agent endpoints — had no throttle at
all. The only middlewares on the parent app were CORS, security headers and
the body-size cap.

WHY THE WINDOW IS PER-CALLER AND NOT GLOBAL
The A2A limiter is deliberately global because that ingress has exactly one
upstream caller (the Authority) and no verified identity at middleware depth;
see its module docstring. Neither holds here. The dashboard has many
concurrent operators, so a global window would let one person's bulk import
lock out everyone else — converting a resource control into an availability
problem, which is the same mistake the shared-IP login lockout made.

WHY THE KEY IS THE SESSION TOKEN, NOT THE USER ID
Authentication happens in the route dependency, well below this middleware, so
there is no verified identity here. Hashing the presented session token gives
a stable per-session bucket without trusting its contents: an attacker can
rotate tokens to get fresh buckets, but a token that does not authenticate
cannot reach any of the expensive handlers this protects — the 401 arrives
first, and unauthenticated floods are bounded by the IP bucket instead.

The limit is generous by design. This is a backstop against a stolen session
spraying job creation, not a quota — a normal operator loading a change detail
page fires a burst of reads and must never notice this.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# Paths that must never be throttled here.
#   /api/health, /api/ready  — liveness probes; throttling them turns a busy
#                              moment into a container restart loop.
#   /api/metrics             — scraped on a fixed interval by design.
#   /a2a-rpc, /.well-known   — the A2A mount has its own limiter, and double
#                              limiting would reject at the wrong layer with
#                              the wrong error shape.
#   /integration-testing     — the tunnel, whose own gating is the
#                              INTEGRATION_TESTING_ENABLED flag.
_EXEMPT_PREFIXES = (
    "/api/health", "/api/ready", "/api/metrics",
    "/a2a-rpc", "/.well-known", "/integration-testing",
)


class _KeyedFixedWindow:
    """Fixed window per key, with opportunistic eviction of stale buckets.

    Per-process, like the A2A limiter's default backend. A multi-replica
    deployment therefore enforces N windows rather than one; that is the same
    trade already documented there, and the same remedy applies if it starts
    to matter.
    """

    def __init__(self, limit: int, window_s: float):
        self._limit = limit
        self._window_s = window_s
        self._buckets: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            # Bound memory: an attacker rotating the key would otherwise grow
            # this dict without limit. Cheap because it only runs when the dict
            # is already large.
            if len(self._buckets) > 4096:
                cutoff = now - self._window_s
                self._buckets = {
                    k: v for k, v in self._buckets.items() if v[1] > cutoff
                }
            count, started = self._buckets.get(key, (0, now))
            if now - started >= self._window_s:
                count, started = 0, now
            count += 1
            self._buckets[key] = (count, started)
            if count > self._limit:
                return False, max(0.0, self._window_s - (now - started))
            return True, 0.0


class DashboardRateLimitMiddleware:
    """Throttle dashboard REST requests per session, falling back to per-IP."""

    def __init__(self, app: ASGIApp, *, limit: int, window_s: float = 60.0):
        self.app = app
        self._window = _KeyedFixedWindow(limit=limit, window_s=window_s)
        self._limit = limit
        self._window_s = window_s
        # Resolved from api.auth rather than duplicated as a literal, so
        # renaming the cookie there cannot silently degrade every browser
        # request to the shared per-IP bucket. Imported here rather than at
        # module scope because core must not import api at import time.
        from app.api.auth import COOKIE_NAME
        self._cookie_name = COOKIE_NAME

    def _key(self, scope: Scope) -> str:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        # Cookie first — the browser path. Hashed so no credential material
        # reaches a dict key, a log line, or a heap dump.
        cookie = headers.get("cookie", "")
        token = ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == self._cookie_name:
                token = value
                break
        if not token:
            auth = headers.get("authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
        if token:
            return "s:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]

        # Unauthenticated: fall back to the peer address. Deliberately does NOT
        # read X-Forwarded-For — see api.auth._client_ip for why an unverified
        # forwarding header is not a safe bucket key. Behind a proxy this
        # collapses to one bucket for all anonymous callers, which is
        # acceptable HERE (unlike the login lockout) because exceeding it costs
        # a 429 on unauthenticated requests for a few seconds rather than
        # locking anyone out of an account.
        client = scope.get("client")
        return "ip:" + (client[0] if client else "unknown")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        allowed, retry_after = self._window.allow(self._key(scope))
        if not allowed:
            from app.core.security_events import emit_security_event
            emit_security_event(
                event_name="dashboard_rate_limit_exceeded",
                severity="medium",
                boundary="dashboard_api",
                decision="rejected",
                reason_code=f"limit={self._limit}/{int(self._window_s)}s",
            )
            body = b'{"detail":"Too many requests. Please retry shortly."}'
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(max(1, int(retry_after))).encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
