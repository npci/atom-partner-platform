# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ITA I-1 — the egress half of the forward direction.

The tunnel actually touches something here: whatever the far side sent is
replayed at a target THIS platform resolved. So these tests are mostly about
what the egress REFUSES — an unknown alias, a path outside the prefixes, a
disabled tunnel, a body over the cap, a deadline with nothing left — because
each of those is a way the tunnel could become someone's SSRF.
"""
from __future__ import annotations

import base64

import httpx
import pytest

from app.a2a_common.integration_contract import (
    ErrorCode, HttpRequestSpec, body_digest, decode_response, encode_request,
)
from app.config import settings
from app.services.integration_testing import egress

EX = "ex-1"
ALIAS = "external_api"
POLICY = (
    '{"external_api": {"scheme": "http", "host": "api.internal", "port": 8080,'
    ' "path_prefixes": ["/v1/"]},'
    ' "stripped": {"scheme": "http", "host": "api.internal",'
    ' "path_prefixes": ["/v1/"], "strip_headers": ["cookie"]}}'
)


@pytest.fixture(autouse=True)
def _tunnel_on(monkeypatch):
    monkeypatch.setattr(settings, "integration_testing_enabled", True, raising=False)
    monkeypatch.setattr(settings, "integration_testing_allowlist", POLICY, raising=False)
    monkeypatch.setattr(settings, "integration_testing_target_timeout_s", 60.0, raising=False)
    monkeypatch.setattr(settings, "integration_testing_max_body_bytes", 1024 * 1024, raising=False)
    monkeypatch.setattr(settings, "integration_testing_max_hops", 1, raising=False)


def _payload(**kw):
    spec = kw.pop("request_spec", HttpRequestSpec("GET", "/v1/ping"))
    return encode_request(exchange_id=EX, alias=kw.pop("alias", ALIAS),
                          request=spec, **kw)


def _stub_transport(monkeypatch, handler):
    """Route httpx through a stub so no socket is opened."""
    real_client = httpx.Client

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **kw)

    monkeypatch.setattr(egress.httpx, "Client", factory)


# ── the happy path ───────────────────────────────────────────────────────────

def test_forward_exchange_reaches_the_resolved_target(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = request.content
        return httpx.Response(201, headers={"X-Reply": "yes"}, content=b"pong")

    _stub_transport(monkeypatch, handler)
    out = decode_response(egress.perform_exchange(_payload(
        request_spec=HttpRequestSpec("POST", "/v1/pay", body=b"hello"))))

    assert seen["url"] == "http://api.internal:8080/v1/pay"
    assert seen["method"] == "POST" and seen["body"] == b"hello"
    assert out.response.status == 201
    assert out.response.body == b"pong"
    assert ("x-reply", "yes") in [(k.lower(), v) for k, v in out.response.headers]
    assert not out.failed


def test_query_string_reaches_the_target_byte_identically(monkeypatch):
    """The whole reason the simulator migration depends on this tunnel."""
    seen = {}
    _stub_transport(monkeypatch, lambda r: (seen.__setitem__("url", str(r.url)),
                                            httpx.Response(200))[1])
    query = "pack=CHG-4711%403&a=1&a=2&unknown=keep"
    egress.perform_exchange(_payload(
        request_spec=HttpRequestSpec("GET", "/v1/x", query=query)))
    assert seen["url"] == f"http://api.internal:8080/v1/x?{query}"


def test_binary_body_round_trips_through_the_egress(monkeypatch):
    blob = bytes(range(256))
    _stub_transport(monkeypatch, lambda r: httpx.Response(200, content=blob))
    out = decode_response(egress.perform_exchange(_payload(
        request_spec=HttpRequestSpec("POST", "/v1/bin", body=blob))))
    assert out.response.body == blob


def test_response_digest_is_computed_over_the_returned_bytes(monkeypatch):
    _stub_transport(monkeypatch, lambda r: httpx.Response(200, content=b"abc"))
    payload = egress.perform_exchange(_payload())
    assert payload["response"]["body_sha256"] == body_digest(b"abc")
    assert base64.b64decode(payload["response"]["body_b64"]) == b"abc"


def test_hop_by_hop_headers_are_not_forwarded_to_the_target(monkeypatch):
    seen = {}
    _stub_transport(monkeypatch, lambda r: (seen.__setitem__("h", dict(r.headers)),
                                            httpx.Response(200))[1])
    egress.perform_exchange(_payload(request_spec=HttpRequestSpec(
        "GET", "/v1/x",
        # `Connection: close` is deliberate: httpx sets its OWN
        # `connection: keep-alive`, so asserting the header is absent proves
        # nothing. A distinctive value httpx would never choose distinguishes
        # "the tunnel forwarded it" from "the client added its own".
        headers=(("Connection", "close"), ("Transfer-Encoding", "chunked"),
                 ("Authorization", "Bearer t"), ("Host", "old.example")))))
    assert seen["h"].get("connection") != "close", "hop-by-hop header was forwarded"
    # httpx never adds this one, so absence here is entirely our doing.
    assert "transfer-encoding" not in {k.lower() for k in seen["h"]}
    # Authorization IS forwarded — deliberate transparency (§5.3).
    assert seen["h"]["authorization"] == "Bearer t"
    # Host is recomputed for the NEW connection, not carried from the old one.
    assert seen["h"]["host"] == "api.internal:8080"


def test_per_alias_strip_headers_are_honoured(monkeypatch):
    seen = {}
    _stub_transport(monkeypatch, lambda r: (seen.__setitem__("h", dict(r.headers)),
                                            httpx.Response(200))[1])
    egress.perform_exchange(_payload(alias="stripped", request_spec=HttpRequestSpec(
        "GET", "/v1/x", headers=(("Cookie", "s=1"), ("Authorization", "Bearer t")))))
    assert "cookie" not in {k.lower() for k in seen["h"]}
    assert seen["h"]["authorization"] == "Bearer t"


# ── what it refuses ──────────────────────────────────────────────────────────

def test_disabled_tunnel_refuses_before_resolving_anything(monkeypatch):
    monkeypatch.setattr(settings, "integration_testing_enabled", False, raising=False)
    out = decode_response(egress.perform_exchange(_payload()))
    assert out.failed and out.error["code"] == ErrorCode.TUNNEL_DISABLED


def test_unknown_alias_is_refused_with_no_fallback(monkeypatch):
    _stub_transport(monkeypatch, lambda r: pytest.fail("must not call a target"))
    out = decode_response(egress.perform_exchange(_payload(alias="nope")))
    assert out.failed and out.error["code"] == ErrorCode.UNKNOWN_ALIAS


def test_path_outside_the_allowed_prefixes_is_refused(monkeypatch):
    _stub_transport(monkeypatch, lambda r: pytest.fail("must not call a target"))
    out = decode_response(egress.perform_exchange(_payload(
        request_spec=HttpRequestSpec("GET", "/admin/shutdown"))))
    assert out.failed and out.error["code"] == ErrorCode.PATH_NOT_ALLOWED


def test_hop_limit_is_enforced_on_the_egress(monkeypatch):
    _stub_transport(monkeypatch, lambda r: pytest.fail("must not call a target"))
    out = decode_response(egress.perform_exchange(_payload(hop=2)))
    assert out.failed and out.error["code"] == ErrorCode.HOP_LIMIT_EXCEEDED


def test_digest_mismatch_is_refused_before_replaying_bytes(monkeypatch):
    _stub_transport(monkeypatch, lambda r: pytest.fail("must not replay tampered bytes"))
    payload = _payload(request_spec=HttpRequestSpec("POST", "/v1/pay", body=b"real"))
    payload["request"]["body_b64"] = base64.b64encode(b"tampered").decode()
    out = decode_response(egress.perform_exchange(payload))
    assert out.failed and out.error["code"] == ErrorCode.DIGEST_MISMATCH


def test_target_timeout_is_reported_as_such(monkeypatch):
    def boom(request):
        raise httpx.ConnectTimeout("too slow", request=request)

    _stub_transport(monkeypatch, boom)
    out = decode_response(egress.perform_exchange(_payload()))
    assert out.failed and out.error["code"] == ErrorCode.TARGET_TIMEOUT


def test_unreachable_target_is_reported_as_such(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    _stub_transport(monkeypatch, boom)
    out = decode_response(egress.perform_exchange(_payload()))
    assert out.failed and out.error["code"] == ErrorCode.TARGET_UNREACHABLE


def test_oversize_target_response_is_refused_before_going_on_the_wire(monkeypatch):
    monkeypatch.setattr(settings, "integration_testing_max_body_bytes", 10, raising=False)
    _stub_transport(monkeypatch, lambda r: httpx.Response(200, content=b"x" * 100))
    out = decode_response(egress.perform_exchange(_payload()))
    assert out.failed and out.error["code"] == ErrorCode.PAYLOAD_TOO_LARGE


# ── the deadline (§6) ────────────────────────────────────────────────────────

def test_deadline_shrinks_the_timeout_below_our_ceiling(monkeypatch):
    seen = {}
    real_client = egress.httpx.Client

    def factory(*a, **kw):
        seen["timeout"] = kw.get("timeout")
        kw["transport"] = httpx.MockTransport(lambda r: httpx.Response(200))
        return real_client(*a, **kw)

    monkeypatch.setattr(egress.httpx, "Client", factory)
    egress.perform_exchange(_payload(deadline_ms=5000))
    assert seen["timeout"] == 5.0, "the caller's remaining budget must win when smaller"


def test_our_ceiling_wins_when_the_deadline_is_larger(monkeypatch):
    seen = {}
    real_client = egress.httpx.Client

    def factory(*a, **kw):
        seen["timeout"] = kw.get("timeout")
        kw["transport"] = httpx.MockTransport(lambda r: httpx.Response(200))
        return real_client(*a, **kw)

    monkeypatch.setattr(egress.httpx, "Client", factory)
    monkeypatch.setattr(settings, "integration_testing_target_timeout_s", 30.0, raising=False)
    egress.perform_exchange(_payload(deadline_ms=999_000))
    assert seen["timeout"] == 30.0


def test_exhausted_deadline_fails_fast_without_calling(monkeypatch):
    _stub_transport(monkeypatch, lambda r: pytest.fail("must not start a doomed call"))
    out = decode_response(egress.perform_exchange(_payload(deadline_ms=10)))
    assert out.failed and out.error["code"] == ErrorCode.TARGET_TIMEOUT


# ── executor wiring ──────────────────────────────────────────────────────────

def test_handler_is_registered_and_declared_blocking():
    from app.a2a_common.partner_executor import (
        HANDLER_TASK_TYPES, build_handler_registry,
    )

    assert "http_exchange_request" in HANDLER_TASK_TYPES
    handler = build_handler_registry()["http_exchange_request"]
    assert getattr(handler, "run_in_thread", False) is True, (
        "the egress blocks for up to the target timeout; without run_in_thread "
        "the executor would hold the event loop for a minute"
    )


def test_executor_dispatches_blocking_handlers_off_the_loop():
    """Source pin on the branch itself: the thread dispatch is invisible to a
    functional test that never measures loop latency."""
    import inspect

    from app.a2a_common import partner_executor

    src = inspect.getsource(partner_executor.PartnerAgentExecutor.execute)
    assert "run_in_thread" in src
    assert "anyio.to_thread.run_sync" in src
