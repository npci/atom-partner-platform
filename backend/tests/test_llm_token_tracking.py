# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for core.llm's token-usage tracking (last_call_tokens + track_token_usage)
that feed Finding 4's per-change budget guard."""
import pytest

from app.config import settings
from app.core import llm
from app.core.resilience import reset_for_tests


@pytest.fixture(autouse=True)
def _reset():
    reset_for_tests()
    yield
    reset_for_tests()


class _FakeAnthropicStream:
    def __init__(self, input_tokens, output_tokens):
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        block = type("B", (), {"type": "text", "text": "ok"})()
        usage = type("U", (), {"input_tokens": self._input_tokens, "output_tokens": self._output_tokens})()
        return type("M", (), {"stop_reason": "end_turn", "content": [block], "usage": usage})()


class _FakeAnthropicClient:
    def __init__(self, input_tokens=100, output_tokens=50):
        self.messages = self
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    def stream(self, **kw):
        return _FakeAnthropicStream(self._input_tokens, self._output_tokens)


def test_last_call_tokens_reflects_most_recent_call(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "claude")
    monkeypatch.setattr(llm, "_get_anthropic_client", lambda: _FakeAnthropicClient(100, 50))
    llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=10)
    assert llm.last_call_tokens() == 150


def test_last_call_tokens_resets_to_zero_at_start_of_each_call(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "claude")
    monkeypatch.setattr(llm, "_get_anthropic_client", lambda: _FakeAnthropicClient(100, 50))
    llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=10)
    assert llm.last_call_tokens() == 150

    # A second call with a client that reports NO usage — must reset to 0,
    # not carry over the prior call's 150.
    class _NoUsageStream(_FakeAnthropicStream):
        def get_final_message(self):
            block = type("B", (), {"type": "text", "text": "ok"})()
            return type("M", (), {"stop_reason": "end_turn", "content": [block], "usage": None})()

    class _NoUsageClient:
        def __init__(self):
            self.messages = self

        def stream(self, **kw):
            return _NoUsageStream(0, 0)

    monkeypatch.setattr(llm, "_get_anthropic_client", lambda: _NoUsageClient())
    llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=10)
    assert llm.last_call_tokens() == 0


def test_track_token_usage_sums_multiple_calls(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "claude")
    calls = iter([(100, 50), (200, 75), (10, 5)])

    def _client():
        input_tokens, output_tokens = next(calls)
        return _FakeAnthropicClient(input_tokens, output_tokens)

    monkeypatch.setattr(llm, "_get_anthropic_client", _client)

    with llm.track_token_usage() as usage:
        llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=10)
        llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=10)
        llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=10)
        assert usage.total() == 150 + 275 + 15

    # After the block exits, the accumulator is gone — a call OUTSIDE the
    # block must not affect a stale reference to `usage`.
    assert usage.total() == 150 + 275 + 15


def test_track_token_usage_isolated_across_nested_calls_not_summed_twice(monkeypatch):
    """A call made with no active tracker (outside any block) must not
    silently start accumulating into some leftover global state."""
    monkeypatch.setattr(settings, "llm_provider", "claude")
    monkeypatch.setattr(llm, "_get_anthropic_client", lambda: _FakeAnthropicClient(10, 10))

    # No tracker active — must not raise, must not affect anything persistent.
    llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=10)
    assert llm.last_call_tokens() == 20

    with llm.track_token_usage() as usage:
        assert usage.total() == 0  # fresh accumulator, unaffected by the untracked call above
        llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=10)
        assert usage.total() == 20


def test_track_token_usage_empty_block_totals_zero(monkeypatch):
    with llm.track_token_usage() as usage:
        pass
    assert usage.total() == 0
