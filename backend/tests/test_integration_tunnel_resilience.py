# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ITA I-5, partner side — sweeper exclusion and the per-alias egress gates.

The live exposure this closes: `send_task_async`'s failure path queued EVERY
task type for retry, so a failed tunnelled POST would have been replayed by
`outbound_retry.run_sweep` as a duplicate business call on the authority's
side. Both layers are pinned here — the enqueue guard and the sweep's
abandon-don't-replay for rows that predate the guard.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import app.npci_client as npci_client
from app.a2a_common.integration_contract import (
    ErrorCode, HttpRequestSpec, decode_response, encode_request,
)
from app.config import settings
from app.models import OutboundA2ARetry
from app.services import outbound_retry
from app.services.integration_testing import egress


# ── the enqueue guard ────────────────────────────────────────────────────────

def _failing_dispatch(monkeypatch):
    async def boom(db, *a, **k):
        raise RuntimeError("wire down")

    monkeypatch.setattr(npci_client, "_dispatch_wire", boom)


def test_failed_tunnel_send_is_not_queued_for_retry(db_session, monkeypatch):
    _failing_dispatch(monkeypatch)
    result = npci_client.send_task(db_session, "http_exchange_request", "chg-1",
                                   {"exchange_id": "ex-1"})
    assert result is None, "the failure still reports to the tunnel's caller"
    assert db_session.query(OutboundA2ARetry).count() == 0, \
        "a queued tunnel exchange would be replayed as a duplicate business call"


def test_ordinary_failed_sends_still_queue(db_session, monkeypatch):
    _failing_dispatch(monkeypatch)
    assert npci_client.send_task(db_session, "query", "chg-1", {"q": 1}) is None
    assert db_session.query(OutboundA2ARetry).count() == 1


# ── the sweep abandons pre-existing tunnel rows ──────────────────────────────

def test_sweep_abandons_queued_tunnel_rows_without_dispatching(db_session, monkeypatch):
    from datetime import datetime, timedelta, timezone

    db_session.add(OutboundA2ARetry(
        change_id="chg-1", task_type="http_exchange_request",
        payload={"exchange_id": "ex-1"}, attempts=0, status="pending",
        next_retry_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    db_session.commit()

    async def must_not_run(*a, **k):
        pytest.fail("a tunnelled exchange must never be replayed")

    monkeypatch.setattr(npci_client, "_dispatch_wire", must_not_run)
    counts = outbound_retry.run_sweep(db_session, max_attempts=6)

    assert counts["abandoned"] == 1 and counts["delivered"] == 0
    row = db_session.query(OutboundA2ARetry).one()
    assert row.status == "abandoned"
    assert "duplicate" in (row.last_error or "")


# ── the per-alias gates (mirror of the NPCI egress — spot coverage) ──────────

POLICY = '{"cb": {"scheme": "http", "host": "api.internal", "path_prefixes": ["/v1/"]}}'


@pytest.fixture
def _tunnel_on(monkeypatch):
    monkeypatch.setattr(settings, "integration_testing_enabled", True, raising=False)
    monkeypatch.setattr(settings, "integration_testing_allowlist", POLICY, raising=False)
    monkeypatch.setattr(settings, "integration_testing_breaker_failure_threshold", 2, raising=False)
    monkeypatch.setattr(settings, "integration_testing_breaker_cooldown_s", 300.0, raising=False)
    monkeypatch.setattr(settings, "integration_testing_max_concurrent_per_alias", 1, raising=False)
    egress._reset_gates_for_tests()
    yield
    egress._reset_gates_for_tests()


def _payload():
    return encode_request(exchange_id="ex-1", alias="cb",
                          request=HttpRequestSpec("GET", "/v1/ping"))


def test_breaker_opens_and_refuses_fast(_tunnel_on, monkeypatch):
    calls = {"n": 0}
    real_client = httpx.Client

    def factory(*a, **kw):
        def failing(request):
            calls["n"] += 1
            raise httpx.ConnectError("dead", request=request)
        kw["transport"] = httpx.MockTransport(failing)
        return real_client(*a, **kw)

    monkeypatch.setattr(egress.httpx, "Client", factory)
    for _ in range(2):
        egress.perform_exchange(_payload())
    out = decode_response(egress.perform_exchange(_payload()))
    assert out.failed and out.error["code"] == ErrorCode.CIRCUIT_OPEN
    assert calls["n"] == 2, "an open circuit must not touch the target"


def test_saturated_alias_is_refused_with_its_own_code(_tunnel_on, monkeypatch):
    real_client = httpx.Client

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(lambda r: httpx.Response(200))
        return real_client(*a, **kw)

    monkeypatch.setattr(egress.httpx, "Client", factory)
    _, bulkhead = egress._gate_for("cb")
    slot = bulkhead.acquire(timeout=1.0)
    slot.__enter__()
    try:
        out = decode_response(egress.perform_exchange(_payload()))
        assert out.failed and out.error["code"] == ErrorCode.BULKHEAD_SATURATED
    finally:
        slot.__exit__(None, None, None)
