# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The certification trigger, Stage 1 (ITA I-6, §3.5).

One versioned HTTP call asking the system under test to ORIGINATE a case. The
platform generates the External API's code but its deployment is out of scope
and its URL is supplied by a user — so this client calls exactly one address a
human typed (`partner_settings` keys `cert_trigger_url` / `cert_trigger_secret`,
validated at entry), and Stage 2's generated `__cert/v1/trigger` handler
replaces the hand-written stub without this side noticing.

THE LOAD-BEARING DETAIL: the trigger returns **202 and never a verdict**. It
says only "start"; the outcome arrives separately as the app's real outbound
call travelling through the tunnel, reported via cert_case_result. If the
trigger returned a result, an app could report a pass without ever making the
call, and the certification would be testing the trigger rather than the
implementation. `fire_trigger` therefore returns only accepted/not-accepted.

`reply_via` is an ALIAS for the same reason §2 gives everywhere: the system
under test is told WHICH name to call; its own tunnel ingress resolves it. No
authority address reaches the application.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

import httpx

logger = logging.getLogger(__name__)

__all__ = ["fire_trigger"]

# The trigger is a small control message and must not linger: the case's real
# execution happens elsewhere on the suite deadline's clock, not this one's.
_TRIGGER_TIMEOUT_S = 10.0


def fire_trigger(
    trigger_url: str,
    trigger_secret: str | None,
    *,
    test_case_id: str,
    cert_context: Mapping[str, Any],
    case_data: Mapping[str, Any] | None,
    reply_via: str,
) -> bool:
    """Ask the system under test to originate `test_case_id`. Returns whether
    the trigger ACCEPTED (2xx with no verdict semantics) — never an outcome.
    """
    # SSRF (SAST F-002), defence in depth. `api/dashboard/settings.py` rejects a
    # private-space `cert_trigger_url` at save time, but this is the call that
    # actually dials, and it fires automatically on every inbound
    # `cert_execution_start` with no human approval. Re-check here so rows
    # written before that validation existed — or by any future write path that
    # forgets it — cannot turn a certification run into a credentialed probe of
    # the internal network. Imported locally: npci_client pulls in the whole
    # settings/DB stack, which this module otherwise has no need of.
    from app.npci_client import _is_private_url

    # unresolved=False: a destination policy, not a liveness check — the rig
    # being down must fail in the transport below (logged as "unreachable"),
    # not be reported as an SSRF refusal.
    if _is_private_url(trigger_url, unresolved=False):
        logger.error(
            "cert trigger REFUSED for case=%s: configured cert_trigger_url resolves "
            "into blocked (loopback/link-local/private) address space. Re-save it in "
            "Settings, or approve the host via NPCI_SSRF_ALLOWED_HOSTS.",
            test_case_id,
        )
        return False

    headers = {}
    if trigger_secret:
        headers["Authorization"] = f"Bearer {trigger_secret}"
    body = {
        "test_case_id": test_case_id,
        "cert_context": dict(cert_context or {}),
        "case_data": dict(case_data or {}),
        "reply_via": reply_via,
    }
    try:
        with httpx.Client(timeout=_TRIGGER_TIMEOUT_S, follow_redirects=False) as client:
            reply = client.post(trigger_url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("cert trigger unreachable for case=%s: %s: %s",
                       test_case_id, type(exc).__name__, exc)
        return False

    if 200 <= reply.status_code < 300:
        logger.info("cert trigger accepted case=%s (HTTP %d)",
                    test_case_id, reply.status_code)
        return True
    logger.warning("cert trigger refused case=%s: HTTP %d",
                   test_case_id, reply.status_code)
    return False
