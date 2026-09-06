# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Fixed-window rate limiter for the A2A ingress, with an optional shared backend.

Two backends behind one tiny `allow()` interface:

  * `_FixedWindow`      — in-memory, per-process. The default, and the only
                          thing most partner forks need.
  * `_RedisFixedWindow` — INCR + EXPIRE against a shared Redis. Enabled by
                          setting `PARTNER_RATE_LIMIT_REDIS_URL`. This is what
                          makes a multi-worker / multi-replica deployment
                          enforce ONE limit instead of N independent ones.

WHY REDIS IS OPTIONAL AND NOT REQUIRED
Most partner stacks don't run redis — the same reason `hmac_middleware.py`
gives for having no redis nonce store. This platform is a reference
implementation that partner organisations fork and deploy in their own
environment, so making redis mandatory would impose infrastructure on every
fork to solve a problem only horizontally-scaled deployments have. The import
is lazy and failure to import is not an error; a fork that never sets the URL
behaves exactly as before.

WHY A REDIS OUTAGE DEGRADES RATHER THAN FAILS OPEN
The obvious two options are both bad at an H3 boundary: failing open drops
rate limiting entirely at the moment infrastructure is already unhealthy, and
failing closed turns a redis blip into an A2A outage. Because the in-process
window still exists, there is a third option — fall back to it. A redis
outage then costs the SHARED property (N workers enforce N windows again),
which is exactly the pre-redis behaviour, rather than costing the control.

WHY THE WINDOW IS GLOBAL AND NOT PER-CALLER
`mount.py` wraps this middleware OUTSIDE the HMAC and JWT middlewares on
purpose, so a flood is rejected before any body buffering or signature
verification. That means no *verified* caller identity exists at this point.
Keying on an unverified JWT `sub` would be strictly worse than a global
window: an attacker could mint a new `sub` per request and get an unlimited
number of buckets. A global window is the correct shape here, and it is also
sufficient — this platform has exactly one upstream caller (the Authority).
Per-partner keying belongs on the Authority side, which is the end that talks
to many partners.

security_architecture_skills.md §4.2 (rate limits), §11.3 (rate limiting as
a mandatory resilience pattern), §19.1 (H3 interfaces require rate limiting).
"""
from __future__ import annotations

import json
import logging
import threading
import time

from starlette.types import ASGIApp, Receive, Scope, Send

from ._security_events import emit_security_event

logger = logging.getLogger(__name__)


class _FixedWindow:
    """Fixed-window counter (simpler and cheap enough at this scale than a
    true sliding log; resets every `window_s` seconds)."""

    def __init__(self, limit: int, window_s: float = 1.0):
        self.limit = max(1, limit)
        self.window_s = window_s
        self._lock = threading.Lock()
        self._window_start = time.monotonic()
        self._count = 0

    def allow(self) -> tuple[bool, float]:
        """Returns (allowed, retry_after_s)."""
        with self._lock:
            now = time.monotonic()
            if now - self._window_start >= self.window_s:
                self._window_start = now
                self._count = 0
            self._count += 1
            if self._count > self.limit:
                retry_after = self.window_s - (now - self._window_start)
                return False, max(retry_after, 0.0)
            return True, 0.0

    def reset(self) -> None:
        with self._lock:
            self._window_start = time.monotonic()
            self._count = 0


class _RedisFixedWindow:
    """Shared fixed-window counter: one key per time bucket, INCR + EXPIRE.

    Same shape as the Authority platform's proven limiter
    (`core/admin_rate_limit.py`), so both ends of the A2A boundary throttle by
    the same mechanism rather than by two hand-rolled variants.

    The bucket is derived from wall-clock time (`time.time() // window_s`)
    rather than a monotonic clock, because the whole point is that separate
    processes agree on which bucket they are in. Modest clock skew between
    workers costs at most one bucket boundary.

    `fallback` is the in-process window used when redis is unreachable — see
    the module docstring for why degrading beats failing open or closed.
    """

    def __init__(self, client, limit: int, window_s: float, fallback: _FixedWindow,
                 key_prefix: str = "partner:a2a:ratelimit"):
        self._client = client
        self.limit = max(1, limit)
        # Whole seconds: the bucket index must be identical across processes,
        # and a fractional window makes that needlessly fragile.
        self.window_s = max(1, int(window_s))
        self._fallback = fallback
        self._key_prefix = key_prefix

    def allow(self) -> tuple[bool, float]:
        bucket = int(time.time() // self.window_s)
        key = f"{self._key_prefix}:{bucket}"
        try:
            count = self._client.incr(key)
            if count == 1:
                # +1s of slack so a key cannot expire while its own bucket is
                # still the current one.
                self._client.expire(key, self.window_s + 1)
        except Exception as e:  # noqa: BLE001 — degrade to per-process, never to nothing
            logger.warning(
                "a2a rate limiter: redis error (%s) — falling back to the "
                "in-process window for this request. The limit is still "
                "enforced, but per worker rather than shared.", e,
            )
            return self._fallback.allow()
        if count > self.limit:
            return False, float(self.window_s)
        return True, 0.0


def _build_redis_client(url: str):
    """Return a redis client for `url`, or None if one cannot be created.

    Lazy import: `redis` is an optional dependency (see module docstring), so
    an ImportError here is an ordinary outcome, not a misconfiguration.
    """
    if not url:
        return None
    try:
        import redis  # noqa: PLC0415 — optional dependency, imported on demand
    except ImportError:
        logger.warning(
            "PARTNER_RATE_LIMIT_REDIS_URL is set but the `redis` package is not "
            "installed — the A2A rate limit stays per-process. Install redis to "
            "enable the shared limiter."
        )
        return None
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
        return client
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "a2a rate limiter: cannot reach redis at the configured URL (%s) — "
            "starting with the in-process window. The shared limiter will not be "
            "active until redis is reachable and the process restarts.", e,
        )
        return None


def shared_limiter_configured() -> bool:
    """True when this deployment is configured for a SHARED rate-limit window.

    `core/runtime.validate_single_instance()` uses this to decide whether a
    multi-worker boot is safe: the per-process limiter is what makes multiple
    workers multiply the effective limit, so a shared backend removes the
    reason that boot is refused.
    """
    try:
        from app.config import settings
        return bool(getattr(settings, "partner_rate_limit_redis_url", "") or "")
    except Exception:  # noqa: BLE001
        return False


class A2ARateLimitMiddleware:
    """Global rate limit on the A2A ingress.

    One window for the whole ingress — see the module docstring for why this
    is keyed globally rather than per caller. Shared across processes when
    `PARTNER_RATE_LIMIT_REDIS_URL` is set, per-process otherwise.

    `redis_client` is injectable so the shared path is testable without a live
    redis; production leaves it None and the URL from settings is used.
    """

    def __init__(self, app: ASGIApp, *, limit_rps: int, window_s: float = 1.0,
                 redis_url: str | None = None, redis_client=None):
        self.app = app
        fallback = _FixedWindow(limit=limit_rps, window_s=window_s)
        if redis_url is None:
            try:
                from app.config import settings
                redis_url = getattr(settings, "partner_rate_limit_redis_url", "") or ""
            except Exception:  # noqa: BLE001 — settings unavailable in some test contexts
                redis_url = ""
        client = redis_client if redis_client is not None else _build_redis_client(redis_url)
        if client is not None:
            self._window = _RedisFixedWindow(client, limit_rps, window_s, fallback)
            self.shared = True
            logger.info(
                "a2a rate limiter: shared redis window active (limit=%s per %ss)",
                limit_rps, max(1, int(window_s)),
            )
        else:
            self._window = fallback
            self.shared = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        allowed, retry_after = self._window.allow()
        if not allowed:
            emit_security_event(
                event_name="rate_limit_exceeded",
                severity="medium",
                boundary="a2a_inbound",
                decision="throttled",
            )
            body = json.dumps({"error": "rate_limited", "detail": "Too many requests."}).encode()
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(int(retry_after) + 1).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return
        await self.app(scope, receive, send)
