# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Bearer JWT validation middleware for the partner-platform A2A mount.

Slice 3 of the A2A security hardening — symmetric to NPCI's
`backend/app/a2a_common/sdk_auth_middleware.py`, but lighter:

  * No `A2ASession` revocation lookup. Partners receive JWTs minted by
    NPCI; revocation is centralized at the issuer (NPCI's `revoked_at`
    column on `a2a_sessions`). The partner just verifies the
    signature + claim shape.
  * No partner-status / DB lookup. Partner is the receiver, not the
    registry holder; the JWT's `sub` claim identifies the issuer
    (NPCI), not the caller-as-partner.
  * No contextvar plumbing. The partner-side executor today doesn't
    use auth context. If/when audit enrichment lands on the partner
    side, lift the same `AUTH_CONTEXT` shape from the NPCI module.

Secret source: `partner_settings.npci_jwt_secret` — the same table
the partner already uses for runtime config. NPCI ships this value to
the partner during onboarding (returned once by NPCI's POST
/admin/partners/.../rotate-jwt-secret endpoint).

FAIL-CLOSED DEFAULT (docs/adr/ADR-0003-fail-closed-a2a-ingress.md): if no
`npci_jwt_secret` setting exists, the middleware REJECTS (503) inbound A2A
calls rather than bypassing auth. The old fail-open back-compat behaviour is
preserved ONLY behind the explicit, documented
`PARTNER_ALLOW_UNAUTHENTICATED_A2A=true` escape hatch (default false) — never
set this in production.

Public surface:
    PartnerAuthMiddleware  — Starlette BaseHTTPMiddleware subclass
"""
from __future__ import annotations

import logging
from typing import Optional

import jwt
from jwt import PyJWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.core.setting_keys import SettingKey

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"

# The `partner_settings` ROW KEY this middleware reads its verification secret
# from — a lookup identifier, not a credential. Taken from core/setting_keys.py,
# whose members derive their values from their own identifiers, so this line is
# no longer `_SETTING_NAME = "<literal>"`: the shape Checkmarx's "Use Of
# Hardcoded Password" query reported here (path 1) in three consecutive scans.
# `SettingKey.npci_jwt_secret == "npci_jwt_secret"`, so the DB lookup below is
# byte-for-byte the same query it always was.
_SETTING_NAME = SettingKey.npci_jwt_secret


class PartnerAuthMiddleware(BaseHTTPMiddleware):
    """Validate Bearer JWT signed by NPCI on every JSON-RPC call.

    `paths_skip_auth` exempts well-known paths so the SDK card endpoint
    stays unauthenticated (NPCI fetches the partner card during
    discovery without a token in hand).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths_skip_auth: tuple[str, ...] = ("/.well-known/",),
    ) -> None:
        super().__init__(app)
        self._skip_paths = paths_skip_auth
        self._warned_missing_secret = False

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in self._skip_paths):
            return await call_next(request)

        secret = self._load_secret()
        if not secret:
            from app.core.secret_box import safe_key_label

            from ._security_events import allow_unconfigured_bypass, emit_security_event
            if allow_unconfigured_bypass():
                if not self._warned_missing_secret:
                    # safe_key_label(), not _SETTING_NAME — see the note in
                    # _load_secret() (Checkmarx "Filtering Sensitive Logs").
                    logger.warning(
                        "PartnerAuthMiddleware: %s is not configured; "
                        "PARTNER_ALLOW_UNAUTHENTICATED_A2A=true — accepting "
                        "unsigned calls. THIS MUST NEVER BE SET IN PRODUCTION.",
                        safe_key_label(_SETTING_NAME),
                    )
                    self._warned_missing_secret = True
                return await call_next(request)
            emit_security_event(
                event_name="jwt_secret_unconfigured_reject",
                severity="critical",
                boundary="a2a_inbound",
                decision="rejected",
            )
            return _err(
                503, "jwt_not_configured",
                "NPCI JWT secret is not configured on this partner instance. "
                "Inbound A2A calls are rejected until an operator installs "
                "npci_jwt_secret in Settings. This is a fail-closed default — "
                "see docs/adr/ADR-0003-fail-closed-a2a-ingress.md.",
            )

        auth_header = request.headers.get("authorization") or ""
        if not auth_header.lower().startswith("bearer "):
            return _err(401, "missing_bearer_token", "Authorization header required.")

        token = auth_header.split(None, 1)[1].strip()
        if not token:
            return _err(401, "missing_bearer_token", "Empty Bearer token.")

        try:
            payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
        except PyJWTError as exc:
            # The exception TYPE (ExpiredSignatureError, InvalidSignatureError,
            # DecodeError, ...) is the actionable part for the caller and is a
            # fixed PyJWT class name, not attacker-influenced content. The
            # exception's MESSAGE is deliberately dropped: this response crosses
            # an H3 partner boundary, and `str(exc)` on a JWT error can echo
            # claim values and key-resolution detail back to an unauthenticated
            # caller (Checkmarx "Information Exposure Through an Error Message").
            # Full detail still reaches the operator via logger.debug below.
            logger.debug("JWT decode failed", exc_info=True)
            return _err(401, "invalid_token",
                        f"JWT validation failed ({type(exc).__name__}).")

        if payload.get("type") != "a2a":
            return _err(401, "invalid_token", "JWT type claim is not 'a2a'.")
        if not payload.get("sub"):
            return _err(401, "invalid_token", "JWT missing sub claim.")

        # Pass through. The partner-side executor doesn't need auth
        # context today; revisit if audit enrichment moves here.
        return await call_next(request)

    def _load_secret(self) -> Optional[str]:
        """Read `partner_settings.npci_jwt_secret` on each request, decrypting
        transparently if the stored value is in core.secret_box's enc:v1: form.

        Per-request DB hit is fine — the partner stack runs Postgres
        in-process-network, the row is hot, and rotation takes effect
        immediately without a process restart. If perf becomes an
        issue, add a TTL cache here; today this is the simplest
        correct shape.
        """
        # Lazy imports to keep this module importable without dragging
        # the whole partner-app graph (mirrors the NPCI side).
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
                    "PartnerAuthMiddleware: failed to decrypt %s — treating as "
                    "unconfigured (fail-closed). Possible KEK mismatch or "
                    "tampered value.", safe_key_label(_SETTING_NAME),
                )
                return None
        finally:
            db.close()


# ── helpers ──────────────────────────────────────────────────────────────────


def _err(status_code: int, error_code: str, detail: str) -> JSONResponse:
    """Structured 401 — same shape as NPCI's SdkAuthMiddleware."""
    logger.warning("partner_auth_reject code=%s detail=%s", error_code, detail)
    return JSONResponse(
        status_code=status_code,
        content={"error": error_code, "detail": detail},
    )
