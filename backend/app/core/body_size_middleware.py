# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Global ASGI backstop for inbound request body size.

Finding 9 (security_architecture_skills.md §4.1/§11.1): the A2A mount's
`PartnerHmacMiddleware` already enforces a bounded, streaming-aware read
limit on `/a2a-rpc/*`. This middleware is defense-in-depth for every OTHER
route in the app (dashboard API, auth, feasibility, users) — a fast
`Content-Length`-based rejection before the request body is ever read, so a
route outside the A2A mount cannot be used to force unbounded in-memory
buffering either.

Deliberately Content-Length-based (not a streaming read-and-count like the
HMAC middleware's) — this is a cheap, universal backstop; routes that
themselves stream large uploads should apply the same reasoning individually
if the platform ever adds one.
"""
from __future__ import annotations

import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


class MaxBodySizeMiddleware:
    """Rejects (413) any request whose `Content-Length` header exceeds
    `max_bytes` before it reaches any route handler. A request with no
    `Content-Length` (e.g. chunked transfer-encoding) is passed through
    unchanged — the more specific, streaming-aware guard on the A2A mount
    (`PartnerHmacMiddleware`) is what protects that path; this middleware is a
    fast-path backstop for the common case."""

    def __init__(self, app: ASGIApp, *, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError:
                length = None
            if length is not None and length > self.max_bytes:
                logger.warning(
                    "MaxBodySizeMiddleware: rejecting %s %s (content-length=%d > %d)",
                    scope.get("method"), scope.get("path"), length, self.max_bytes,
                )
                body = json.dumps({
                    "detail": f"Request body exceeds the {self.max_bytes}-byte limit."
                }).encode()
                await send({
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                })
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return

        await self.app(scope, receive, send)
