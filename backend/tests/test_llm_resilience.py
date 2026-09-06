# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""End-to-end test that `call_llm()` is actually wrapped by the circuit
breaker + bulkhead (Findings 1/2/3: docs/adr/ADR-0001-llm-circuit-breaker-and-bulkhead.md).

Uses the same fake-client pattern as test_llm_ainxt_compat.py so no network
or real SDK call happens.
"""
import pytest

from app.config import settings
from app.core import hostility, llm
from app.core.resilience import CircuitOpenError, reset_for_tests


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    # Rebuild the hostility registry from the (now-restored) real settings and
    # clear breaker/bulkhead singletons so the next test starts clean — a test
    # that monkeypatches settings.llm_* would otherwise leave a stale
    # BOUNDARIES snapshot for tests that run after it.
    hostility.validate_at_startup()
    reset_for_tests()


class _FailingClient:
    """Raises on every call — simulates a provider outage."""
    def __init__(self):
        self.messages = self
        self.calls = 0

    def stream(self, **kw):
        self.calls += 1
        raise RuntimeError("simulated provider outage")


def test_circuit_opens_after_threshold_and_fails_fast(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "claude")
    monkeypatch.setattr(settings, "llm_cb_failure_threshold", 3)
    monkeypatch.setattr(settings, "llm_cb_cooldown_s", 30)
    hostility.validate_at_startup()  # rebuild BOUNDARIES from the patched settings

    fake = _FailingClient()
    monkeypatch.setattr(llm, "_get_anthropic_client", lambda: fake)

    # First `failure_threshold` calls hit the real (fake) dispatch and fail.
    for _ in range(3):
        with pytest.raises(RuntimeError, match="simulated provider outage"):
            llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=10)
    assert fake.calls == 3

    # The next call must fail FAST via CircuitOpenError, WITHOUT dispatching
    # to the (fake) client again — proves the breaker, not the client, is
    # what's rejecting now.
    with pytest.raises(CircuitOpenError):
        llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=10)
    assert fake.calls == 3  # unchanged — no additional dispatch happened


def test_bulkhead_rejects_beyond_concurrent_limit(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_concurrent_calls", 1)
    hostility.validate_at_startup()  # rebuild BOUNDARIES from the patched setting

    from app.core.resilience import bulkhead_for

    bulkhead = bulkhead_for("llm_provider")
    # Hold the only slot open, then attempt a second call_llm — it must be
    # rejected by the bulkhead (fails BEFORE any provider dispatch, so no
    # client/API-key setup is needed for this assertion).
    with bulkhead.acquire(timeout=1.0):
        with pytest.raises(RuntimeError, match="saturated"):
            llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=10)


def test_successful_call_keeps_circuit_closed(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "claude")

    class _FakeStream:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def get_final_message(self):
            block = type("B", (), {"type": "text", "text": "ok"})()
            return type("M", (), {"stop_reason": "end_turn", "content": [block]})()

    class _FakeClient:
        def __init__(self):
            self.messages = self
        def stream(self, **kw):
            return _FakeStream()

    monkeypatch.setattr(llm, "_get_anthropic_client", lambda: _FakeClient())
    out = llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=10)
    assert out == "ok"

    from app.core.resilience import breaker_for
    assert breaker_for("llm_provider").state == "closed"


def test_all_sdk_client_constructors_pass_explicit_timeout(monkeypatch):
    """Finding 3 — every SDK client must be built with an explicit timeout,
    not the library default."""
    monkeypatch.setattr(settings, "partner_anthropic_api_key", "k")
    monkeypatch.setattr(settings, "partner_openai_api_key", "k")
    monkeypatch.setattr(settings, "partner_ainxt_api_key", "k")
    monkeypatch.setattr(settings, "ainxt_base_url", "https://example.invalid")

    llm._get_anthropic_client.cache_clear()
    llm._get_openai_client.cache_clear()
    llm._get_ainxt_client.cache_clear()
    llm._get_ainxt_anthropic_client.cache_clear()

    anthropic_client = llm._get_anthropic_client()
    assert anthropic_client.timeout == llm._LLM_TIMEOUT

    openai_client = llm._get_openai_client()
    assert openai_client.timeout == llm._LLM_TIMEOUT

    ainxt_client = llm._get_ainxt_client()
    assert ainxt_client.timeout == llm._LLM_TIMEOUT

    ainxt_anthropic_client = llm._get_ainxt_anthropic_client()
    assert ainxt_anthropic_client.timeout == llm._LLM_TIMEOUT

    llm.reset_clients_for_tests()
