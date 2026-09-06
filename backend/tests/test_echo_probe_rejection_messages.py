# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The echo probe must name the layer that actually rejected it.

NPCI answers EVERY rejection on /a2a-rpc/rpc with 401 — its JWT middleware and
its HMAC envelope middleware both do, and the envelope middleware emits no
other 4xx besides 413. The probe used to branch on the status code, testing
`"401" in str(exc)` first, so every envelope failure was reported as
"Bearer JWT rejected" and the operator went and rotated the wrong secret. The
`"403"` branch that carried the HMAC message was unreachable against any real
authority.

These tests pin the discrimination to the structured `error` code in the
response body, which is the only thing that separates the two layers.

The stubs below mirror the a2a SDK's real wrapping: `A2AClientError` carrying
only the status line in its message, with the `httpx.HTTPStatusError` that
still holds the body on `__cause__`. A test that raises `HTTPStatusError`
directly would pass without proving the chain is walked.
"""
from __future__ import annotations

import httpx
import pytest

from app.npci_client import _send_echo_probe


def _sdk_error(status: int, body: dict | None, *, text: str | None = None):
    """Build the exception shape the SDK actually raises for an HTTP error."""
    request = httpx.Request("POST", "https://npci.example/a2a-rpc/rpc")
    if body is not None:
        response = httpx.Response(status, json=body, request=request)
    else:
        response = httpx.Response(status, text=text or "", request=request)
    cause = httpx.HTTPStatusError(
        f"Client error '{status}' for url '{request.url}'",
        request=request, response=response,
    )
    # What a2a/client/transports/http_helpers.py raises: the body is gone from
    # the message, and survives only via __cause__.
    outer = RuntimeError(f"HTTP Error {status}: {cause}")
    outer.__cause__ = cause
    return outer


@pytest.fixture()
def probe(monkeypatch):
    """Drive _send_echo_probe by choosing what the transport raises."""
    def _run(exc):
        def boom(coro=None, *a, **kw):
            # The probe builds the coroutine before handing it over; closing it
            # keeps "coroutine was never awaited" out of the run.
            if hasattr(coro, "close"):
                coro.close()
            raise exc

        monkeypatch.setattr("app.npci_client._run_portably", boom)
        return _send_echo_probe("https://npci.example", "tok", "sekret")

    return _run


# ── the defect: an HMAC 401 must not read as a JWT problem ───────────────────

@pytest.mark.parametrize("code", [
    "signature_mismatch", "envelope_invalid", "missing_envelope_headers",
    "invalid_envelope", "replay_detected", "nonce_check_unavailable",
])
def test_hmac_rejections_are_reported_as_hmac_even_though_they_are_401(probe, code):
    ok, detail = probe(_sdk_error(401, {"error": code}))
    assert ok is False
    assert "HMAC" in detail, f"{code} was not attributed to the envelope layer"
    assert "JWT" not in detail, \
        "an envelope failure reported as a JWT failure sends the operator to " \
        "the wrong secret — this is the defect"


def test_signature_mismatch_points_at_the_signing_secret(probe):
    ok, detail = probe(_sdk_error(401, {"error": "signature_mismatch"}))
    assert ok is False
    assert "Settings" in detail and "401" in detail
    assert "403" not in detail, "the authority never answers this with 403"


@pytest.mark.parametrize("code", [
    "invalid_token", "missing_bearer_token", "session_unknown",
    "session_revoked", "session_expired", "partner_unknown", "partner_inactive",
])
def test_jwt_rejections_are_still_reported_as_jwt(probe, code):
    ok, detail = probe(_sdk_error(401, {"error": code}))
    assert ok is False
    assert "JWT" in detail and code in detail
    assert "HMAC" not in detail


# ── clock skew earns its own message ─────────────────────────────────────────

def test_clock_skew_is_not_blamed_on_a_secret(probe):
    """The one failure here that no secret rotation can fix. Folded into the
    generic envelope message, the operator rotates a good secret and the clock
    stays wrong."""
    ok, detail = probe(_sdk_error(401, {"error": "timestamp_skew"}))
    assert ok is False
    assert "clock" in detail.lower()
    assert "300" in detail, "say how far out of the window is too far"
    assert "No secret is wrong" in detail


def test_a_secret_missing_on_the_npci_side_says_so(probe):
    """Signed here, but NPCI holds no secret for this partner — the remedy is
    on the authority, not in this platform's Settings."""
    ok, detail = probe(_sdk_error(401, {"error": "hmac_secret_not_configured"}))
    assert ok is False
    assert "NPCI" in detail and "NO signing secret" in detail


# ── fallbacks stay intact ────────────────────────────────────────────────────

def test_a_404_still_names_the_missing_mount(probe):
    ok, detail = probe(_sdk_error(404, None, text="Not Found"))
    assert ok is False
    assert "404" in detail and "SDK mount" in detail


def test_an_unrecognised_code_is_reported_verbatim_not_guessed_at(probe):
    ok, detail = probe(_sdk_error(429, {"error": "rate_limited"}))
    assert ok is False
    assert "rate_limited" in detail and "429" in detail
    assert "JWT" not in detail and "HMAC" not in detail


def test_a_transport_failure_with_no_response_falls_back_to_the_message(probe):
    """No middleware was ever reached — there is no body to discriminate on."""
    ok, detail = probe(httpx.ConnectError("connection refused"))
    assert ok is False
    assert "connection refused" in detail


def test_a_non_json_error_body_does_not_crash_the_probe(probe):
    ok, detail = probe(_sdk_error(502, None, text="<html>bad gateway</html>"))
    assert ok is False
    assert detail, "the probe must still return an explanation"


# ── the assumption the whole fix rests on ────────────────────────────────────

def test_the_installed_sdk_really_does_preserve_the_response_body():
    """`_describe_rejection` reads the body off `__cause__` because the a2a SDK
    discards it from the exception message. If a future SDK release wrapped
    without `from e`, every message here would silently degrade to the raw
    string fallback — and this suite would still pass, because the stubs above
    build the chain by hand.

    So drive the SDK's OWN `handle_http_exceptions` with a real 401 and check
    the chain survives. This is the test that fails on an SDK upgrade.
    """
    import httpx
    from a2a.client.transports.http_helpers import handle_http_exceptions

    from app.npci_client import _CLOCK_SKEW_MSG, _describe_rejection

    request = httpx.Request("POST", "https://npci.example/a2a-rpc/rpc")
    response = httpx.Response(
        401, json={"error": "timestamp_skew", "detail": "HMAC envelope check failed."},
        request=request,
    )

    with pytest.raises(Exception) as caught:      # noqa: PT011 — the SDK's own type
        with handle_http_exceptions():
            response.raise_for_status()

    assert "timestamp_skew" not in str(caught.value), \
        "premise check: the SDK is expected to drop the body from the message"
    assert _describe_rejection(caught.value) == _CLOCK_SKEW_MSG
