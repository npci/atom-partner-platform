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

Back-compat: if no `npci_jwt_secret` setting exists, the middleware
bypasses auth and logs a one-time WARN. This preserves existing
partner deployments until they install the new secret.

Public surface:
    PartnerAuthMiddleware  — Starlette BaseHTTPMiddleware subclass
"""
from __future__ import annotations

import logging
from typing import Optional

from jose import JWTError, jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
_SETTING_NAME = "npci_jwt_secret"


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
            from app.config import settings
            if settings.app_env == "production":
                return _err(401, "a2a_secret_not_configured",
                            f"`{_SETTING_NAME}` is not installed; refusing unsigned A2A calls.")
            # Back-compat path (non-production): NPCI hasn't shipped the
            # per-partner secret yet. Pass through with a one-time warning.
            if not self._warned_missing_secret:
                logger.warning(
                    "PartnerAuthMiddleware: no `%s` configured in "
                    "partner_settings; accepting unsigned A2A calls. "
                    "Install the secret NPCI returned at partner-create "
                    "or rotate-jwt-secret time to enable enforcement.",
                    _SETTING_NAME,
                )
                self._warned_missing_secret = True
            return await call_next(request)

        auth_header = request.headers.get("authorization") or ""
        if not auth_header.lower().startswith("bearer "):
            return _err(401, "missing_bearer_token", "Authorization header required.")

        token = auth_header.split(None, 1)[1].strip()
        if not token:
            return _err(401, "missing_bearer_token", "Empty Bearer token.")

        try:
            payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
        except JWTError as exc:
            return _err(401, "invalid_token", f"JWT decode failed: {exc}")

        if payload.get("type") != "a2a":
            return _err(401, "invalid_token", "JWT type claim is not 'a2a'.")
        if not payload.get("sub"):
            return _err(401, "invalid_token", "JWT missing sub claim.")

        # Pass through. The partner-side executor doesn't need auth
        # context today; revisit if audit enrichment moves here.
        return await call_next(request)

    def _load_secret(self) -> Optional[str]:
        """Read `partner_settings.npci_jwt_secret` on each request.

        Per-request DB hit is fine — the partner stack runs SQLite
        in-process, the row is hot, and rotation takes effect
        immediately without a process restart. If perf becomes an
        issue, add a TTL cache here; today this is the simplest
        correct shape.
        """
        # Lazy imports to keep this module importable without dragging
        # the whole partner-app graph (mirrors the NPCI side).
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


def _err(status_code: int, error_code: str, detail: str) -> JSONResponse:
    """Structured 401 — same shape as NPCI's SdkAuthMiddleware."""
    logger.warning("partner_auth_reject code=%s detail=%s", error_code, detail)
    return JSONResponse(
        status_code=status_code,
        content={"error": error_code, "detail": detail},
    )
