"""Bank -> NPCI sends: token cache, bounded retry, reused TLS context / agent card.

Network is faked at `httpx.post` and `send_a2a_message`; no NPCI is needed.
"""
import asyncio
import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app import npci_client
from app.a2a_common import client as a2a_client


def _jwt(ttl: float) -> str:
    claims = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + ttl}).encode()).decode().rstrip("=")
    return f"h.{claims}.s"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    npci_client._tokens.clear()
    a2a_client._cards.clear()
    monkeypatch.setattr(npci_client, "_get_a2a_base_url", lambda db: "http://npci")
    monkeypatch.setattr(npci_client, "_get_api_key", lambda db, bank_org_id=None: "key-1")
    monkeypatch.setattr(npci_client, "_get_setting", lambda db, k, default="": default)
    yield
    npci_client._tokens.clear()
    a2a_client._cards.clear()


def _fake_post(monkeypatch, tokens):
    posts = []

    def post(url, json=None, timeout=None):
        posts.append(json["api_key"])
        return SimpleNamespace(status_code=200, text="", json=lambda: {"jwt": tokens[len(posts) - 1]})

    monkeypatch.setattr(httpx, "post", post)
    return posts


# ── token cache ──────────────────────────────────────────────────────────────

def test_token_is_fetched_once_while_it_is_valid(monkeypatch):
    posts = _fake_post(monkeypatch, [_jwt(900), _jwt(900)])
    first = npci_client.authenticate(None)
    assert npci_client.authenticate(None) == first and len(posts) == 1


def test_a_token_about_to_expire_is_not_reused(monkeypatch):
    posts = _fake_post(monkeypatch, [_jwt(30), _jwt(900)])   # inside the 60 s skew
    npci_client.authenticate(None)
    npci_client.authenticate(None)
    assert len(posts) == 2


def test_a_different_api_key_gets_its_own_token(monkeypatch):
    posts = _fake_post(monkeypatch, [_jwt(900), _jwt(900)])
    npci_client.authenticate(None)
    monkeypatch.setattr(npci_client, "_get_api_key", lambda db, bank_org_id=None: "key-2")
    npci_client.authenticate(None)
    assert posts == ["key-1", "key-2"]


def test_invalidate_forces_a_new_handshake(monkeypatch):
    posts = _fake_post(monkeypatch, [_jwt(900), _jwt(900)])
    npci_client.authenticate(None)
    npci_client.invalidate_token(None)
    npci_client.authenticate(None)
    assert len(posts) == 2


def test_a_failed_handshake_is_not_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1) or SimpleNamespace(
        status_code=401, text="no", json=lambda: {}))
    assert npci_client.authenticate(None) is None and npci_client.authenticate(None) is None
    assert len(calls) == 2


# ── retry ────────────────────────────────────────────────────────────────────

def _status_error(code):
    return httpx.HTTPStatusError(
        "boom", request=httpx.Request("POST", "http://npci"), response=httpx.Response(code))


def _wire(monkeypatch, failures):
    """send_a2a_message raises each of `failures` in turn, then succeeds."""
    seen, delays, tokens = [], [], []
    queue = list(failures)

    async def fake_send(**kw):
        seen.append(kw["data"]["message_id"])
        tokens.append(kw["auth_header"])
        if queue:
            raise queue.pop(0)
        return {}

    monkeypatch.setattr(npci_client, "send_a2a_message", fake_send)
    monkeypatch.setattr(npci_client.time, "sleep", delays.append)
    return seen, delays, tokens


def _send():
    return npci_client.send_task(None, "cert_verdict_acceptance", None, {"case_ids": ["A"]})


def test_a_transient_failure_is_retried_with_the_same_message_id(monkeypatch):
    monkeypatch.setattr(npci_client, "authenticate", lambda db, bank_org_id=None: "tok")
    seen, delays, _ = _wire(monkeypatch, [httpx.ConnectError("refused"), _status_error(503)])
    assert _send() == {"status": "delivered"}
    assert len(seen) == 3 and len(set(seen)) == 1            # one message_id -> NPCI dedups
    assert delays == [1.0, 2.0]                              # exponential backoff


def test_a_client_error_is_not_retried(monkeypatch):
    monkeypatch.setattr(npci_client, "authenticate", lambda db, bank_org_id=None: "tok")
    seen, delays, _ = _wire(monkeypatch, [_status_error(400)])
    assert _send() is None and len(seen) == 1 and delays == []


def test_retries_are_bounded(monkeypatch):
    monkeypatch.setattr(npci_client, "authenticate", lambda db, bank_org_id=None: "tok")
    seen, delays, _ = _wire(monkeypatch, [_status_error(503)] * 10)
    assert _send() is None
    assert len(seen) == npci_client._SEND_ATTEMPTS and delays == [1.0, 2.0, 4.0]


def test_an_sdk_wrapped_network_error_is_retried(monkeypatch):
    from a2a.client.errors import AgentCardResolutionError
    monkeypatch.setattr(npci_client, "authenticate", lambda db, bank_org_id=None: "tok")
    seen, delays, _ = _wire(monkeypatch, [
        AgentCardResolutionError("Network communication error fetching agent card from http://npci: x")])
    assert _send() == {"status": "delivered"} and len(seen) == 2


def test_a_401_triggers_one_fresh_handshake(monkeypatch):
    issued = iter(["old", "new"])
    monkeypatch.setattr(npci_client, "authenticate", lambda db, bank_org_id=None: next(issued))
    monkeypatch.setattr(npci_client, "invalidate_token", lambda db, bank_org_id=None: None)
    seen, delays, tokens = _wire(monkeypatch, [_status_error(401)])
    assert _send() == {"status": "delivered"}
    assert tokens == ["Bearer old", "Bearer new"] and delays == []


def test_a_second_401_is_a_real_rejection(monkeypatch):
    monkeypatch.setattr(npci_client, "authenticate", lambda db, bank_org_id=None: "tok")
    monkeypatch.setattr(npci_client, "invalidate_token", lambda db, bank_org_id=None: None)
    seen, _, _ = _wire(monkeypatch, [_status_error(401)] * 5)
    assert _send() is None and len(seen) == 2


# ── agent card + TLS context ─────────────────────────────────────────────────

def test_the_tls_context_is_built_once():
    assert a2a_client._ssl_context() is a2a_client._ssl_context()


def test_the_agent_card_is_fetched_once_and_rebased(monkeypatch):
    fetched = []
    card = SimpleNamespace(supported_interfaces=[SimpleNamespace(url="http://npci_backend:8000/a2a-rpc/rpc")])

    class Resolver:
        def __init__(self, http, base_url):
            pass

        async def get_agent_card(self):
            fetched.append(1)
            return card

    monkeypatch.setattr(a2a_client, "A2ACardResolver", Resolver)

    async def twice():
        a = await a2a_client._agent_card(None, "http://host.docker.internal:8010")
        b = await a2a_client._agent_card(None, "http://host.docker.internal:8010")
        return a, b

    a, b = asyncio.run(twice())
    assert a is b and len(fetched) == 1
    assert card.supported_interfaces[0].url == "http://host.docker.internal:8010/a2a-rpc/rpc"


def test_an_expired_card_is_fetched_again(monkeypatch):
    fetched = []

    class Resolver:
        def __init__(self, http, base_url):
            pass

        async def get_agent_card(self):
            fetched.append(1)
            return SimpleNamespace(supported_interfaces=[])

    monkeypatch.setattr(a2a_client, "A2ACardResolver", Resolver)
    a2a_client._cards["http://npci"] = (time.monotonic() - 1, object())
    asyncio.run(a2a_client._agent_card(None, "http://npci"))
    assert len(fetched) == 1
