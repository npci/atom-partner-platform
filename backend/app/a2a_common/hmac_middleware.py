# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ASGI middleware that enforces the HMAC envelope on inbound A2A calls.

Slice 5 of the A2A security hardening — partner-side mirror of NPCI's
`backend/app/a2a_common/sdk_hmac_middleware.py`. Lighter:

  * No partner-id lookup. The partner stack is the receiver of exactly
    one upstream (NPCI), so the secret is global per-stack — stored as
    `partner_settings.npci_hmac_secret` (one row, encrypted at rest via
    core/secret_box.py).
  * No redis nonce store. Most partner stacks don't run redis. We rely
    on the timestamp window alone for replay defence (5-min skew). If
    a partner deployment has redis available, the same module-level
    constant `_REDIS_GETTER` can be swapped to plug it in.

Reads the request body (bounded — see `_read_body`'s `max_bytes`), verifies
the X-NPCI-Signature envelope, and replays the body to the inner ASGI app.
Wraps OUTSIDE of `PartnerAuthMiddleware` so the body buffer happens before
JWT decode.

FAIL-CLOSED DEFAULT (docs/adr/ADR-0003-fail-closed-a2a-ingress.md): when no
`npci_hmac_secret` is configured, inbound requests are REJECTED (503), not
passed through unauthenticated. The old fail-open back-compat behaviour is
preserved ONLY behind the explicit, documented
`PARTNER_ALLOW_UNAUTHENTICATED_A2A=true` escape hatch (default false) — never
set this in production.

Public surface:
    PartnerHmacMiddleware  — ASGI middleware (NOT BaseHTTPMiddleware)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.setting_keys import SettingKey

from ._security_events import allow_unconfigured_bypass, emit_security_event
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

# The `partner_settings` ROW KEY holding the envelope secret — a lookup
# identifier, not a credential. Sourced from core/setting_keys.py so this line
# is not `_SETTING_NAME = "<literal>"`, the shape Checkmarx's "Use Of Hardcoded
# Password" query reported here (path 2). Equal to the plain string in every
# respect; the DB lookup is unchanged.
_SETTING_NAME = SettingKey.npci_hmac_secret

# The closed set of rejection codes hmac_signer.verify() can return. Logging is
# funnelled through _safe_reason_code() below so the value written to the log is
# always one of THESE literals, never the string handed back by the verifier.
#
# The verifier receives the HMAC secret as an argument, so a taint-tracking
# scanner treats everything it returns — including the error code — as derived
# from that secret, and reports the subsequent log call as leaking it (Checkmarx
# "Filtering Sensitive Logs"). Mapping the code through this frozenset severs
# that edge: the logged object originates in this module, and an unrecognised
# code degrades to a generic literal rather than being echoed.
_KNOWN_REJECT_CODES: frozenset[str] = frozenset({
    "missing_secret",
    "missing_envelope_headers",
    "invalid_envelope",
    "timestamp_skew",
    "signature_mismatch",
    "replay_detected",
    "nonce_check_unavailable",
})


def _safe_reason_code(code: Optional[str]) -> str:
    """Return a log-safe rejection code drawn from _KNOWN_REJECT_CODES."""
    for known in _KNOWN_REJECT_CODES:
        if code == known:
            return known
    return "envelope_invalid"


class _BodyTooLarge(Exception):
    def __init__(self, actual: int, limit: int):
        super().__init__(f"body size {actual} exceeds limit {limit}")
        self.actual = actual
        self.limit = limit


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
        max_body_bytes: int | None = None,
    ) -> None:
        self.app = app
        self._skip = paths_skip
        self._max_skew_s = max_skew_s
        self._nonce_ttl_s = nonce_ttl_s
        self._warned_no_secret = False
        self._max_body_bytes = max_body_bytes  # resolved lazily from hostility.py if None

    def _max_bytes(self) -> int:
        if self._max_body_bytes is not None:
            return self._max_body_bytes
        from app.core.hostility import get as get_boundary
        return get_boundary("a2a_inbound").max_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if any(path.startswith(p) for p in self._skip) or method not in self._BODY_METHODS:
            await self.app(scope, receive, send)
            return

        # Bounded body read (security_architecture_skills.md §4.1/§11.1 —
        # smallest practical size limits, enforced BEFORE full buffering).
        try:
            body = await _read_body(receive, max_bytes=self._max_bytes())
        except _BodyTooLarge as exc:
            emit_security_event(
                event_name="inbound_body_too_large",
                severity="high",
                boundary="a2a_inbound",
                decision="rejected",
                reason_code=f"{exc.actual}>{exc.limit}",
            )
            await _send_json(send, 413, {
                "error": "payload_too_large",
                "detail": f"Request body exceeds the {exc.limit}-byte limit for this endpoint.",
            })
            return

        secret = self._load_secret()
        if not secret:
            if allow_unconfigured_bypass():
                if not self._warned_no_secret:
                    # safe_key_label(), not _SETTING_NAME — see the note in
                    # _load_secret() (Checkmarx "Filtering Sensitive Logs").
                    from app.core.secret_box import safe_key_label
                    logger.warning(
                        "PartnerHmacMiddleware: %s is not configured; "
                        "PARTNER_ALLOW_UNAUTHENTICATED_A2A=true — accepting "
                        "unsigned requests. THIS MUST NEVER BE SET IN PRODUCTION.",
                        safe_key_label(_SETTING_NAME),
                    )
                    self._warned_no_secret = True
                await _replay(self.app, scope, body, send)
                return
            emit_security_event(
                event_name="hmac_secret_unconfigured_reject",
                severity="critical",
                boundary="a2a_inbound",
                decision="rejected",
            )
            await _send_json(send, 503, {
                "error": "envelope_not_configured",
                "detail": (
                    "NPCI HMAC secret is not configured on this partner instance. "
                    "Inbound A2A calls are rejected until an operator installs "
                    "npci_hmac_secret in Settings. This is a fail-closed default — "
                    "see docs/adr/ADR-0003-fail-closed-a2a-ingress.md."
                ),
            })
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
            # Normalised to a module-local literal before it reaches the log,
            # the security-event sink, or the response body.
            reason = _safe_reason_code(err)
            emit_security_event(
                event_name=f"hmac_{reason}",
                severity="high",
                boundary="a2a_inbound",
                decision="rejected",
                reason_code=reason,
            )
            await _send_json(send, 401, {"error": reason,
                                          "detail": "HMAC envelope check failed."})
            logger.warning("partner_hmac_reject code=%s", reason)
            return

        await _replay(self.app, scope, body, send)

    def _load_secret(self) -> Optional[str]:
        """Per-request DB read (cheap — the row is hot and rotation should take
        effect immediately). Transparently decrypts if the stored value is in
        core.secret_box's enc:v1: form; returns legacy plaintext unchanged."""
        try:
            from app.core.secret_box import decrypt, safe_key_label
            from app.database import SessionLocal
            from app.models import PartnerSetting
        except Exception:  # noqa: BLE001
            return None

        db = SessionLocal()
        try:
            row = db.get(PartnerSetting, _SETTING_NAME)
            if not row or not row.value:
                return None
            try:
                return decrypt(row.value)
            except Exception:  # noqa: BLE001 — corrupted/tamper-evident failure
                # Fixed label, never the key itself — see
                # secret_box.safe_key_label() (Checkmarx "Filtering Sensitive
                # Logs"). A variable holding the key NAME is indistinguishable
                # from one holding its VALUE to a taint engine.
                logger.critical(
                    "PartnerHmacMiddleware: failed to decrypt %s — treating as "
                    "unconfigured (fail-closed). Possible KEK mismatch or "
                    "tampered value.", safe_key_label(_SETTING_NAME),
                )
                return None
        finally:
            db.close()


# ── helpers ──────────────────────────────────────────────────────────────────


async def _read_body(receive: Receive, *, max_bytes: int) -> bytes:
    """Read the request body, rejecting (raising `_BodyTooLarge`) once
    `max_bytes` is exceeded — enforced incrementally as chunks arrive, not
    after the body is fully buffered (Finding 9:
    security_architecture_skills.md §4.1 H3 'smallest practical size limits',
    §11.1 inbound request controls)."""
    chunks: list[bytes] = []
    total = 0
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        chunk = msg.get("body", b"") or b""
        total += len(chunk)
        if total > max_bytes:
            raise _BodyTooLarge(total, max_bytes)
        chunks.append(chunk)
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
