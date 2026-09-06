# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ITA I-6, partner half: the start signal handler and the Stage-1 trigger.

The trigger's load-bearing detail is pinned hard: it returns ACCEPTED or NOT —
never a verdict. If it returned a result, an app could report a pass without
ever making the call, and the certification would be testing the trigger.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.a2a_common.handlers._types import TaskReceiveRequest
from app.a2a_common.handlers.cert_lifecycle import handle_cert_execution_start
from app.models import ChangeTestData, PartnerSetting
from app.services.integration_testing import trigger


def _stub_http(monkeypatch, handler):
    real_client = httpx.Client

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **kw)

    monkeypatch.setattr(trigger.httpx, "Client", factory)


# ── fire_trigger: 202, never a verdict ───────────────────────────────────────

def test_trigger_sends_the_contract_shape_and_returns_accepted(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"accepted": True, "test_case_id": "TC-7"})

    _stub_http(monkeypatch, handler)
    ok = trigger.fire_trigger(
        "https://sut.example/__cert/v1/trigger", "s3cret",
        test_case_id="TC-7",
        cert_context={"cflow_id": "CF-1", "cert_attempt": 2, "initiator": "bank"},
        case_data={"payerVpa": "a@bank"},
        reply_via="a2a://cert_simulator",
    )
    assert ok is True
    assert seen["auth"] == "Bearer s3cret"
    assert seen["body"]["test_case_id"] == "TC-7"
    assert seen["body"]["cert_context"]["cflow_id"] == "CF-1"
    assert seen["body"]["case_data"] == {"payerVpa": "a@bank"}
    assert seen["body"]["reply_via"] == "a2a://cert_simulator", \
        "an alias, never a raw authority URL (§2)"


def test_trigger_returns_only_a_boolean_never_a_verdict(monkeypatch):
    """Even a trigger that (wrongly) answers with a verdict body yields only
    'accepted' — the outcome must arrive through the tunnel, not here."""
    _stub_http(monkeypatch, lambda r: httpx.Response(
        200, json={"accepted": True, "status": "passed", "verdict": "PASS"}))
    result = trigger.fire_trigger("https://sut.example/t", None,
                                  test_case_id="TC-1", cert_context={},
                                  case_data=None, reply_via="a2a://x")
    assert result is True and isinstance(result, bool)


def test_trigger_refusal_and_unreachable_are_not_accepted(monkeypatch):
    _stub_http(monkeypatch, lambda r: httpx.Response(401))
    assert trigger.fire_trigger("https://sut.example/t", None, test_case_id="T",
                                cert_context={}, case_data=None,
                                reply_via="a2a://x") is False

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    _stub_http(monkeypatch, boom)
    assert trigger.fire_trigger("https://sut.example/t", None, test_case_id="T",
                                cert_context={}, case_data=None,
                                reply_via="a2a://x") is False


# ── handle_cert_execution_start ──────────────────────────────────────────────

def _body(case_ids, change_id="chg-1"):
    return TaskReceiveRequest(
        task_type="cert_execution_start", change_id=change_id,
        payload={"case_ids": case_ids, "deadline_ms": 105_000,
                 "simulator_alias": "cert_simulator",
                 "cert_context": {"cflow_id": "CF-1", "cert_attempt": 1,
                                  "initiator": "bank"}})


def test_unconfigured_trigger_is_an_honest_zero_dispatch(db_session):
    reply = handle_cert_execution_start(_body(["TC1", "TC2"]), db_session)
    assert reply["status"] == "accepted"
    assert reply["dispatched"] == 0
    assert "no certification trigger is configured" in reply["summary"]


def test_each_case_fires_the_trigger_with_its_own_data(db_session, monkeypatch):
    db_session.add(PartnerSetting(key="cert_trigger_url",
                                  value="https://sut.example/__cert/v1/trigger"))
    db_session.add(PartnerSetting(key="cert_trigger_secret", value="s3cret"))
    db_session.add(ChangeTestData(change_id="chg-1", tc_id="TC1",
                                  test_data={"payerVpa": "one@bank"}))
    db_session.add(ChangeTestData(change_id="chg-1", tc_id="TC2",
                                  test_data={"payerVpa": "two@bank"}))
    db_session.commit()

    fired = []
    monkeypatch.setattr(
        "app.services.integration_testing.trigger.fire_trigger",
        lambda url, secret, **kw: fired.append({"url": url, "secret": secret, **kw}) or True)

    async def scenario():
        reply = handle_cert_execution_start(_body(["TC1", "TC2"]), db_session)
        await asyncio.sleep(0.05)   # let the _spawn'd workers run
        return reply

    reply = asyncio.run(scenario())
    assert reply["dispatched"] == 2

    by_case = {f["test_case_id"]: f for f in fired}
    assert set(by_case) == {"TC1", "TC2"}
    assert by_case["TC1"]["case_data"] == {"payerVpa": "one@bank"}
    assert by_case["TC2"]["case_data"] == {"payerVpa": "two@bank"}
    assert by_case["TC1"]["reply_via"] == "a2a://cert_simulator"
    # Attribution without timing correlation: the context names the case.
    assert by_case["TC1"]["cert_context"]["test_case_id"] == "TC1"
    assert by_case["TC1"]["cert_context"]["cflow_id"] == "CF-1"


def test_execution_start_is_recognised_inbound_and_handled():
    """Footgun #5: a handled type missing from _INBOUND_TASK_TYPES degrades to
    a generic ack that reports success while doing nothing."""
    from app.a2a_common.partner_executor import (
        _INBOUND_TASK_TYPES, HANDLER_TASK_TYPES, build_handler_registry,
    )

    assert "cert_execution_start" in _INBOUND_TASK_TYPES
    assert "cert_execution_start" in HANDLER_TASK_TYPES
    assert build_handler_registry()["cert_execution_start"] is handle_cert_execution_start
