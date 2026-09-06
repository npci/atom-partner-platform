# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ITA I-4 — the ingress half of the REVERSE direction (partner side).

`forward_exchange` carries an External-API callback to the authority. These
tests stub the async sender (ITA-3's `send_task_async`) and pin what travels —
the §5.1 payload, the verbatim query, the header stripping, the §6 budget —
and how every reply shape is read: the merged ITA-2 receipt, the legacy
`message`-wrapped shape, a far-side error, and no reply at all.

The last test is the I-4 verify bar end to end: route the stubbed wire into
THIS repo's own egress (the byte-identical contract mirror of the authority's)
against an httpx MockTransport, so one test walks
ingress → encode → [wire] → egress → target → encode → [wire] → decode.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import app.npci_client as npci_client
from app.a2a_common.integration_contract import (
    ErrorCode, HttpResponseSpec, encode_error, encode_response,
)
from app.config import settings
from app.services.integration_testing import ingress

EX_RESPONSE = encode_response(
    exchange_id="ignored",  # overwritten per test via _receipt
    response=HttpResponseSpec(status=200, headers=(("X-CB", "1"),), body=b"cb-ok"),
    elapsed_ms=42,
)


@pytest.fixture(autouse=True)
def _tunnel_on(monkeypatch):
    monkeypatch.setattr(settings, "integration_testing_enabled", True, raising=False)
    monkeypatch.setattr(settings, "integration_testing_a2a_timeout_s", 90.0, raising=False)
    monkeypatch.setattr(settings, "integration_testing_target_timeout_s", 60.0, raising=False)
    monkeypatch.setattr(settings, "integration_testing_max_body_bytes", 1024 * 1024, raising=False)


def _receipt(exchange_payload: dict) -> dict:
    """What the authority's executor actually returns since ITA-2: the handler
    dict merged under the executor-owned identity keys."""
    return {**exchange_payload, "task_id": "m-1", "status": "completed",
            "task_type": "http_exchange_request"}


def _stub_send(monkeypatch, reply):
    calls = []

    async def fake(db, task_type, change_id, payload, *, correlation_id=None,
                   timeout=None):
        calls.append({"task_type": task_type, "payload": payload,
                      "timeout": timeout, "correlation_id": correlation_id})
        return reply(payload) if callable(reply) else reply

    monkeypatch.setattr(npci_client, "send_task_async", fake)
    return calls


def _forward(**kw):
    defaults = dict(db=None, alias="npci_simulator", method="POST",
                    path="/cb/notify", query="", headers=[], body=b"ping")
    defaults.update(kw)
    return asyncio.run(ingress.forward_exchange(**defaults))


# ── what travels ─────────────────────────────────────────────────────────────

def test_the_exchange_payload_and_budget_ride_the_wire(monkeypatch):
    calls = _stub_send(monkeypatch, lambda p: _receipt(
        {**EX_RESPONSE, "exchange_id": p["exchange_id"]}))
    result = _forward(query="pack=CHG-4711%403&a=1&a=2")

    assert not result.failed
    sent = calls[0]
    assert sent["task_type"] == "http_exchange_request"
    assert sent["timeout"] == 90.0, "the §6 middle layer must ride the send"
    payload = sent["payload"]
    assert payload["target"]["alias"] == "npci_simulator"
    assert payload["request"]["method"] == "POST"
    assert payload["request"]["path"] == "/cb/notify"
    assert payload["request"]["query"] == "pack=CHG-4711%403&a=1&a=2", \
        "the query must travel VERBATIM (§12.5 — ?pack= selection rides on it)"
    assert payload["deadline_ms"] == 60_000
    assert sent["correlation_id"] == result.exchange_id


def test_hop_by_hop_headers_are_stripped_before_the_wire(monkeypatch):
    calls = _stub_send(monkeypatch, lambda p: _receipt(
        {**EX_RESPONSE, "exchange_id": p["exchange_id"]}))
    _forward(headers=[("Connection", "close"), ("Authorization", "Bearer t")])
    names = {n.lower() for n, _ in calls[0]["payload"]["request"]["headers"]}
    assert "connection" not in names
    assert "authorization" in names, "transparency is the point (§5.3)"


def test_disabled_tunnel_refuses_without_sending(monkeypatch):
    monkeypatch.setattr(settings, "integration_testing_enabled", False, raising=False)
    calls = _stub_send(monkeypatch, lambda p: pytest.fail("must not send"))
    result = _forward()
    assert result.failed and result.error["code"] == ErrorCode.TUNNEL_DISABLED
    assert calls == []


# ── how every reply shape is read ────────────────────────────────────────────

def test_the_merged_receipt_comes_home_as_the_response(monkeypatch):
    _stub_send(monkeypatch, lambda p: _receipt(
        {**EX_RESPONSE, "exchange_id": p["exchange_id"]}))
    result = _forward()
    assert not result.failed
    assert result.response.status == 200
    assert result.response.body == b"cb-ok"
    assert result.elapsed_ms == 42


def test_the_legacy_message_wrapped_shape_is_still_read(monkeypatch):
    """Pre-ITA-2 executors wrapped the dict under `message` — tolerated so a
    version-skewed pair keeps working during a coordinated rollout."""
    _stub_send(monkeypatch, lambda p: {
        "task_id": "m-1", "status": "completed",
        "message": {**EX_RESPONSE, "exchange_id": p["exchange_id"]}})
    result = _forward()
    assert not result.failed and result.response.body == b"cb-ok"


def test_a_far_side_error_surfaces_with_its_code(monkeypatch):
    _stub_send(monkeypatch, lambda p: _receipt(
        encode_error(exchange_id=p["exchange_id"],
                     code=ErrorCode.UNKNOWN_ALIAS, detail="nope")))
    result = _forward()
    assert result.failed and result.error["code"] == ErrorCode.UNKNOWN_ALIAS


def test_no_reply_at_all_is_target_unreachable(monkeypatch):
    _stub_send(monkeypatch, None)
    result = _forward()
    assert result.failed and result.error["code"] == ErrorCode.TARGET_UNREACHABLE


def test_a_bare_delivery_marker_is_target_unreachable(monkeypatch):
    """`{"status": "delivered"}` means the send worked but no exchange payload
    came back — for the tunnel that is a failure with a name, not a success."""
    _stub_send(monkeypatch, {"status": "delivered"})
    result = _forward()
    assert result.failed and result.error["code"] == ErrorCode.TARGET_UNREACHABLE


# ── the I-4 verify bar: the callback scenario end to end ─────────────────────

def test_callback_scenario_end_to_end_through_the_real_contract(monkeypatch):
    """ingress → encode → [wire] → egress → target → encode → [wire] → decode.

    The far side here is THIS repo's own egress — byte-identical contract
    mirror of the authority's (both are vendored `integration_contract` +
    `integration_allowlist` consumers) — with the executor's ITA-2 receipt
    merge reproduced on the boundary. The target is an httpx MockTransport
    standing in for the Simulator's callback API.
    """
    from app.services.integration_testing import egress

    monkeypatch.setattr(settings, "integration_testing_allowlist",
                        '{"npci_simulator": {"scheme": "http", "host": "sim.internal",'
                        ' "port": 8090, "path_prefixes": ["/cb/"]}}', raising=False)
    monkeypatch.setattr(settings, "integration_testing_max_hops", 1, raising=False)

    seen = {}

    def target(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(202, headers={"X-CB-Ack": "yes"}, content=b"accepted")

    real_client = httpx.Client

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(target)
        return real_client(*a, **kw)

    monkeypatch.setattr(egress.httpx, "Client", factory)

    async def wire(db, task_type, change_id, payload, *, correlation_id=None,
                   timeout=None):
        # The authority's executor: thread-dispatched egress, dict merged into
        # the receipt under the executor-owned identity keys (ITA-2).
        result = await asyncio.to_thread(egress.perform_exchange, payload)
        return {**result, "task_id": "m-1", "status": "completed",
                "task_type": task_type}

    monkeypatch.setattr(npci_client, "send_task_async", wire)

    result = _forward(path="/cb/txn", query="pack=CHG-9%403", body=b"<Resp/>")

    assert seen["url"] == "http://sim.internal:8090/cb/txn?pack=CHG-9%403"
    assert seen["body"] == b"<Resp/>"
    assert not result.failed
    assert result.response.status == 202
    assert result.response.body == b"accepted"
    assert ("x-cb-ack", "yes") in [(k.lower(), v) for k, v in result.response.headers]
