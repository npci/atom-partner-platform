# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Structured security-event emission for the A2A ingress middlewares.

Per security_architecture_skills.md §13.2/§13.3 — every auth/authz failure,
config validation failure, and replay/signature failure MUST emit a
structured, alertable event (not just a log line).

Shared by hmac_middleware.py, auth_middleware.py, and rate_limit_middleware.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("security_events")


def allow_unconfigured_bypass() -> bool:
    """True only when the operator has explicitly set the dev-only escape
    hatch (PARTNER_ALLOW_UNAUTHENTICATED_A2A=true). Distinct from
    PARTNER_ALLOW_HTTP (config.py) — this one controls authentication, not
    transport, and defaults to False (fail-closed).

    AR-13 -- never true in production or staging, whatever the flag says.
    `config.py` already refuses to START with the flag set in those
    environments, so reaching this branch means the setting was mutated after
    boot (a DB-backed config override, a test monkeypatch, a reload). That is
    the case worth defending against: the startup guard is the loud check, and
    this is the one that holds if the value changes underneath it.

    See docs/adr/ADR-0003-fail-closed-a2a-ingress.md.
    """
    from app.config import settings
    if not settings.partner_allow_unauthenticated_a2a:
        return False
    if _is_protected_env(settings):
        logger.error(
            "SECURITY_EVENT event=unauthenticated_bypass_refused severity=critical "
            "boundary=a2a_inbound decision=rejected app_env=%s "
            "detail=\"PARTNER_ALLOW_UNAUTHENTICATED_A2A is set but ignored outside "
            "development\"",
            _env_label(settings),
        )
        return False
    return True


def _env_label(settings) -> str:
    """`app_env`, read defensively -- it arrives from the environment and can
    be an unexpected type or carry whitespace."""
    return (str(getattr(settings, "app_env", "") or "")).strip().lower()


def _is_protected_env(settings) -> bool:
    """True for the environments where the auth bypass must never apply.

    Staging is included on purpose: `config.py`'s own comment on the flag says
    "NEVER set this in a production or staging deployment", and a staging
    stack is routinely reachable from the same networks as production while
    holding realistic data. Anything that is not clearly development is
    treated as protected, so a typo (`APP_ENV=prod`, `APP_ENV=Production `)
    fails safe rather than silently enabling the bypass.
    """
    return _env_label(settings) != "development"


def emit_security_event(
    *,
    event_name: str,
    severity: str,
    boundary: str,
    decision: str,
    reason_code: str | None = None,
    correlation_id: str | None = None,
) -> None:
    """Emit a structured security event — the shape defined in
    security_architecture_skills.md §13.3. Logged at CRITICAL/WARNING/INFO so
    it is trivially alertable via a log-based metric or SIEM rule without
    requiring a new sink; upgrade to a dedicated events table/queue when
    volume justifies it (see docs/OPERATIONAL_RUNBOOKS.md §1.1/§2)."""
    payload = {
        "event_name": event_name,
        "severity": severity,
        "boundary": boundary,
        "hostility_tier": "H3",
        "decision": decision,
        "reason_code": reason_code,
        "correlation_id": correlation_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if severity == "critical":
        level = logging.CRITICAL
    elif severity == "high":
        level = logging.ERROR
    elif severity == "medium":
        level = logging.WARNING
    else:
        level = logging.INFO
    logger.log(level, "SECURITY_EVENT %s", payload)
