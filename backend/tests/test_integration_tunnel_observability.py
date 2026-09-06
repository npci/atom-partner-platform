# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""ITA I-9, partner side — the hooks write rows; a row alone diagnoses.

The recorder module is the byte-mirror of the NPCI one (modulo the model
import), so the deep recorder matrix lives there; here the pins are the four
partner call sites and the admin view.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

import app.npci_client as npci_client
from app.a2a_common.handlers._types import TaskReceiveRequest
from app.a2a_common.integration_contract import (
    ErrorCode, HttpRequestSpec, HttpResponseSpec, encode_request, encode_response,
)
from app.config import settings
from app.models import IntegrationExchange
from app.services.integration_testing import egress


def _rows(db):
    return db.query(IntegrationExchange).all()


@pytest.fixture
def _tunnel_on(monkeypatch):
    monkeypatch.setattr(settings, "integration_testing_enabled", True, raising=False)
    monkeypatch.setattr(settings, "integration_testing_allowlist",
                        '{"cb": {"scheme": "http", "host": "api.internal",'
                        ' "path_prefixes": ["/v1/"]}}', raising=False)
    egress._reset_gates_for_tests()
    yield
    egress._reset_gates_for_tests()


def test_egress_handler_records_its_hop(db_session, _tunnel_on, monkeypatch):
    real_client = httpx.Client

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(lambda r: httpx.Response(201, content=b"ok!"))
        return real_client(*a, **kw)

    monkeypatch.setattr(egress.httpx, "Client", factory)
    body = TaskReceiveRequest(
        task_type="http_exchange_request", change_id=None,
        payload=encode_request(exchange_id="ex-1", alias="cb",
                               request=HttpRequestSpec("POST", "/v1/pay", body=b"12345")))
    result = egress.handle_http_exchange_request(body, db_session)
    assert "response" in result

    row = _rows(db_session)[0]
    assert row.direction == "egress" and row.alias == "cb"
    assert row.status == 201
    assert row.request_bytes == 5 and row.response_bytes == 3


def test_egress_handler_records_a_refusal_diagnosably(db_session, _tunnel_on, monkeypatch):
    body = TaskReceiveRequest(
        task_type="http_exchange_request", change_id=None,
        payload=encode_request(exchange_id="ex-2", alias="nope",
                               request=HttpRequestSpec("GET", "/v1/x")))
    egress.handle_http_exchange_request(body, db_session)
    row = _rows(db_session)[0]
    assert row.error_code == ErrorCode.UNKNOWN_ALIAS and row.status is None
    assert (row.alias, row.method, row.path) == ("nope", "GET", "/v1/x")


def test_ingress_records_its_hop_including_dropped_names(db_session, monkeypatch):
    from app.services.integration_testing import ingress

    monkeypatch.setattr(settings, "integration_testing_enabled", True, raising=False)

    async def fake_send(db, task_type, change_id, payload, *, correlation_id=None,
                        timeout=None):
        return {**encode_response(
            exchange_id=payload["exchange_id"],
            response=HttpResponseSpec(status=200, headers=(), body=b"ok"),
            elapsed_ms=9),
            "task_id": "m", "status": "completed", "task_type": task_type}

    monkeypatch.setattr(npci_client, "send_task_async", fake_send)
    result = asyncio.run(ingress.forward_exchange(
        db=db_session, alias="cert_simulator", method="POST", path="/cb/txn",
        query="", body=b"hello", headers=[("Connection", "close")]))
    assert not result.failed
    row = _rows(db_session)[0]
    assert row.direction == "ingress" and row.status == 200
    assert "Connection" in (row.dropped_headers or [])


def test_admin_view_lists_the_rows(db_session):
    from app.api.integration_testing import list_exchanges
    from app.services.integration_testing.observability import record_exchange

    record_exchange(db_session, direction="egress", exchange_id="ex-9",
                    alias="cb", method="GET", path="/v1/x",
                    error_code="target_timeout")
    out = list_exchanges(limit=10, user=SimpleNamespace(id="admin"), db=db_session)
    assert out["exchanges"][0]["exchange_id"] == "ex-9"
    assert out["exchanges"][0]["error_code"] == "target_timeout"


# ── NET-F21: the query string must be recorded, and recorded VERBATIM ────────
#
# Found jointly during the two-sided integration test. Before these, two
# tunnelled exchanges differing ONLY by their query produced identical rows:
# `query` was absent from the table, absent from record_exchange()'s signature,
# and dropped at both call sites — even though the wire had carried it all
# along (integration_contract emits `"query": request.query or ""`).
#
# It matters because certification contract selection rides entirely on
# `?pack=`, and the failure mode both platforms' plans call out — a normalised
# or dropped selector presenting later as "certified against baseline" rather
# than as an error — lived in the one field the telemetry could not see.

@pytest.mark.parametrize("query", [
    "pack=CHG-4711%403",      # %40 must NOT be decoded to '@'
    "pack=baseline@demo",     # a literal '@' must survive too
    "a=1&a=2",                # duplicate keys must not be collapsed
    "pack=a+b",               # '+' must not become a space
    "pack=sha256:32d76f81",   # ':' unescaped
    "",                       # genuinely no query
])
def test_egress_records_the_query_verbatim(db_session, _tunnel_on, monkeypatch, query):
    real_client = httpx.Client

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, content=b"ok"))
        return real_client(*a, **kw)

    monkeypatch.setattr(egress.httpx, "Client", factory)
    body = TaskReceiveRequest(
        task_type="http_exchange_request", change_id=None,
        payload=encode_request(
            exchange_id="ex-q", alias="cb",
            request=HttpRequestSpec("GET", "/v1/pay", query=query)))
    egress.handle_http_exchange_request(body, db_session)

    row = _rows(db_session)[0]
    assert row.query == query, (
        "the query must be stored byte-for-byte as sent; normalising it is the "
        "exact failure that presents later as 'certified against baseline'")


def test_two_hops_differing_only_by_query_are_distinguishable(db_session, _tunnel_on,
                                                              monkeypatch):
    """The regression in one assertion: this is what NET-F21 actually cost."""
    real_client = httpx.Client

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, content=b"ok"))
        return real_client(*a, **kw)

    monkeypatch.setattr(egress.httpx, "Client", factory)
    for i, q in enumerate(["pack=baseline@demo", "pack=CHG-4711%403"]):
        body = TaskReceiveRequest(
            task_type="http_exchange_request", change_id=None,
            payload=encode_request(
                exchange_id=f"ex-dq{i}", alias="cb",
                request=HttpRequestSpec("GET", "/v1/pay", query=q)))
        egress.handle_http_exchange_request(body, db_session)

    rows = _rows(db_session)
    assert len(rows) == 2
    # Identical in every OTHER recorded column — which is precisely why the
    # absence of `query` made them indistinguishable.
    assert rows[0].path == rows[1].path
    assert rows[0].status == rows[1].status
    assert {r.query for r in rows} == {"pack=baseline@demo", "pack=CHG-4711%403"}


def test_null_query_and_empty_query_are_different_facts(db_session, _tunnel_on,
                                                        monkeypatch):
    """NULL means "not recorded" (a row predating the column); "" means the hop
    genuinely carried no query. Collapsing them would reintroduce the ambiguity
    the column exists to remove."""
    real_client = httpx.Client

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(lambda r: httpx.Response(200, content=b"ok"))
        return real_client(*a, **kw)

    monkeypatch.setattr(egress.httpx, "Client", factory)
    body = TaskReceiveRequest(
        task_type="http_exchange_request", change_id=None,
        payload=encode_request(exchange_id="ex-empty", alias="cb",
                               request=HttpRequestSpec("GET", "/v1/pay", query="")))
    egress.handle_http_exchange_request(body, db_session)
    assert _rows(db_session)[0].query == "", "an absent query must be '' , not NULL"

    # A caller that never supplies the field at all still yields NULL.
    from app.services.integration_testing.observability import record_exchange
    record_exchange(db_session, direction="egress", exchange_id="ex-unrecorded",
                    alias="cb", method="GET", path="/v1/pay")
    row = db_session.query(IntegrationExchange).filter_by(
        exchange_id="ex-unrecorded").one()
    assert row.query is None, "an unsupplied query must stay NULL = 'not recorded'"
