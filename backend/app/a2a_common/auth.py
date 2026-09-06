# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Bearer JWT helpers for outbound A2A calls.

Today only the cert_engine handshake uses Bearer JWTs (see
`backend/app/services/a2a_client.py:_get_cert_engine_jwt`). Slice 5
generalises this so every partner type can advertise an `/a2a/auth`
endpoint and the platform exchanges its `api_key` for a short-lived JWT
on first use, refreshes proactively before expiry.

This module is the **client side** only: it fetches and caches tokens.
The receiving server (partner / cert-agent) owns issuance and validation.

Cache shape:
    {partner_id: {"jwt": str, "expires_at": <epoch_seconds>}}

Module-global so reuse survives across requests in the same process. A
worker restart re-handshakes, which is fine — JWT exchange is cheap.
"""
from __future__ import annotations

import time
from typing import Optional

import httpx

_TOKEN_CACHE: dict[str, dict] = {}


async def fetch_bearer_jwt(
    partner_id: str,
    auth_url: str,
    api_key: str,
    *,
    refresh_window_s: int = 60,
    timeout: float = 15.0,
) -> Optional[str]:
    """Fetch (or reuse a cached) JWT from a partner's `/a2a/auth` endpoint.

    Args:
        partner_id:       Cache key — typically `PartnerAgent.id`.
        auth_url:         Full URL of the partner's `/a2a/auth` endpoint.
        api_key:          Static credential exchanged for a JWT.
        refresh_window_s: Refresh proactively this many seconds before
                          the cached token expires. 60s avoids races
                          where a token expires mid-request.
        timeout:          httpx timeout for the auth call itself.

    Returns:
        The JWT string, or None if the auth call fails. Callers must
        treat None as "auth not available" and decide whether to fall
        back to legacy POST or surface the failure.
    """
    cached = _TOKEN_CACHE.get(partner_id)
    now = int(time.time())
    if cached and cached.get("expires_at", 0) - now > refresh_window_s:
        return cached["jwt"]

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(auth_url, json={"api_key": api_key})
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:
        # Network / DNS / timeout / JSON-parse — caller decides what to
        # do. We don't log here because the caller has more context
        # (partner name, endpoint url, attempt count).
        return None

    token = data.get("jwt")
    if not token:
        return None
    _TOKEN_CACHE[partner_id] = {
        "jwt": token,
        "expires_at": now + int(data.get("expires_in", 3600)),
    }
    return token


def reset_cache_for_tests() -> None:
    """Clear the in-memory token cache. Used by unit tests; do not call
    from production code paths."""
    _TOKEN_CACHE.clear()
