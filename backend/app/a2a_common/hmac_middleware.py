"""ASGI middleware that enforces the HMAC envelope on inbound A2A calls.

Slice 5 of the A2A security hardening — partner-side mirror of NPCI's
`backend/app/a2a_common/sdk_hmac_middleware.py`. Lighter:

  * No partner-id lookup. The partner stack is the receiver of exactly
    one upstream (NPCI), so the secret is global per-stack — stored as
    `partner_settings.npci_hmac_secret` (one row).
  * No redis nonce store. Most partner stacks don't run redis. We rely
    on the timestamp window alone for replay defence (5-min skew). If
    a partner deployment has redis available, the same module-level
    constant `_REDIS_GETTER` can be swapped to plug it in.

Reads the full request body, verifies the X-NPCI-Signature envelope,
and replays the body to the inner ASGI app. Wraps OUTSIDE of
`PartnerAuthMiddleware` so the body buffer happens before JWT decode.

Public surface:
    PartnerHmacMiddleware  — ASGI middleware (NOT BaseHTTPMiddleware)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .hmac_signer import (
    DEFAULT_MAX_SKEW_S,
    DEFAULT_NONCE_TTL_S,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
)
from .hmac_signer import (
    verify as hmac_verify,
)

logger = logging.getLogger(__name__)

_SETTING_NAME = "npci_hmac_secret"


class PartnerHmacMiddleware:
    """Verify X-NPCI-Signature on every inbound POST/PUT/PATCH JSON-RPC."""

    _BODY_METHODS = {"POST", "PUT", "PATCH"}

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths_skip: tuple[str, ...] = ("/.well-known/",),
        max_skew_s: int = DEFAULT_MAX_SKEW_S,
        nonce_ttl_s: int = DEFAULT_NONCE_TTL_S,
    ) -> None:
        self.app = app
        self._skip = paths_skip
        self._max_skew_s = max_skew_s
        self._nonce_ttl_s = nonce_ttl_s
        self._warned_no_secret = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if any(path.startswith(p) for p in self._skip) or method not in self._BODY_METHODS:
            await self.app(scope, receive, send)
            return

        body = await _read_body(receive)

        secret = self._load_secret()
        if not secret:
            from app.config import settings
            if settings.app_env == "production":
                await _send_json(send, 401, {
                    "error": "a2a_secret_not_configured",
                    "detail": f"`{_SETTING_NAME}` is not installed; refusing unsigned A2A calls."})
                logger.warning("partner_hmac_reject code=a2a_secret_not_configured")
                return
            if not self._warned_no_secret:
                logger.warning(
                    "PartnerHmacMiddleware: no `%s` configured in "
                    "partner_settings; accepting requests without "
                    "envelope check. Install the secret NPCI returned "
                    "at partner-create or rotate-hmac-secret time.",
                    _SETTING_NAME,
                )
                self._warned_no_secret = True
            await _replay(self.app, scope, body, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        envelope_headers = {
            HEADER_TIMESTAMP: headers.get(HEADER_TIMESTAMP.lower()),
            HEADER_NONCE:     headers.get(HEADER_NONCE.lower()),
            HEADER_SIGNATURE: headers.get(HEADER_SIGNATURE.lower()),
        }

        ok, err = hmac_verify(
            envelope_headers, body, secret,
            redis_client=None,  # partner stack typically has no redis
            max_skew_s=self._max_skew_s,
            nonce_ttl_s=self._nonce_ttl_s,
        )
        if not ok:
            await _send_json(send, 401, {"error": err or "envelope_invalid",
                                          "detail": "HMAC envelope check failed."})
            logger.warning("partner_hmac_reject code=%s", err)
            return

        await _replay(self.app, scope, body, send)

    def _load_secret(self) -> Optional[str]:
        """Per-request DB read (sqlite, in-process — cheap)."""
        try:
            from app.database import SessionLocal
            from app.models import PartnerSetting
        except Exception:  # noqa: BLE001
            return None

        db = SessionLocal()
        try:
            row = db.get(PartnerSetting, _SETTING_NAME)
            return row.value if row and row.value else None
        finally:
            db.close()


# ── helpers ──────────────────────────────────────────────────────────────────


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        chunks.append(msg.get("body", b"") or b"")
        if not msg.get("more_body", False):
            break
    return b"".join(chunks)


async def _replay(app: ASGIApp, scope: Scope, body: bytes, send: Send) -> None:
    sent = False

    async def replay_receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    await app(scope, replay_receive, send)


async def _send_json(send: Send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body, "more_body": False})
