# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""AiNxt compat-mode toggle (Phase 1): LLM_PROVIDER=ainxt routes to the Anthropic
/v1/messages path when AINXT_COMPAT_MODE=anthropic, else the OpenAI /chat/completions
path. Mirrors the NPCI backend's ainxt_compat_mode switch.

Fakes stand in for the provider clients, recording which route ran + the model used,
so no network or real SDK call happens.
"""
import pytest

from app.config import settings
from app.core import llm


# ── Fake clients ─────────────────────────────────────────────────────────────

class _FakeStream:
    def __init__(self, rec):
        self._rec = rec

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        block = type("B", (), {"type": "text", "text": "anthropic-path-ok"})()
        return type("M", (), {"stop_reason": "end_turn", "content": [block]})()


class _FakeAnthropic:
    """Matches the .messages.stream(...).get_final_message() shape _call_claude uses."""
    def __init__(self, rec):
        self._rec = rec
        self.messages = self

    def stream(self, **kw):
        self._rec.update(kw)
        self._rec["route"] = "anthropic"
        return _FakeStream(self._rec)


class _FakeOpenAI:
    """Matches the .chat.completions.create(...) shape _call_openai_compat uses."""
    def __init__(self, rec):
        self._rec = rec
        self.chat = self
        self.completions = self

    def create(self, **kw):
        self._rec.update(kw)
        self._rec["route"] = "openai"
        msg = type("Msg", (), {"content": "openai-path-ok"})()
        choice = type("Ch", (), {"message": msg, "finish_reason": "stop"})()
        return type("R", (), {"choices": [choice]})()


# ── Tests ────────────────────────────────────────────────────────────────────

def test_ainxt_anthropic_mode_routes_to_v1_messages(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "ainxt")
    monkeypatch.setattr(settings, "ainxt_compat_mode", "anthropic")
    monkeypatch.setattr(settings, "ainxt_messages_model", "claude-sonnet-4-6")
    monkeypatch.setattr(settings, "partner_ainxt_api_key", "k")
    rec: dict = {}
    monkeypatch.setattr(llm, "_get_ainxt_anthropic_client", lambda: _FakeAnthropic(rec))

    out = llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=100)

    assert out == "anthropic-path-ok"
    assert rec["route"] == "anthropic"                       # /v1/messages path taken
    assert rec["model"] == "claude-sonnet-4-6"               # messages_model, not ainxt_model
    assert llm.get_model("ainxt") == "claude-sonnet-4-6"


def test_ainxt_openai_mode_routes_to_chat_completions(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "ainxt")
    monkeypatch.setattr(settings, "ainxt_compat_mode", "openai")
    monkeypatch.setattr(settings, "ainxt_model", "gpt-4o")
    monkeypatch.setattr(settings, "partner_ainxt_api_key", "k")
    rec: dict = {}
    monkeypatch.setattr(llm, "_get_ainxt_client", lambda: _FakeOpenAI(rec))

    out = llm.call_llm("sys", [{"role": "user", "content": "hi"}], max_tokens=100)

    assert out == "openai-path-ok"
    assert rec["route"] == "openai"                          # /chat/completions path taken
    assert rec["model"] == "gpt-4o"
    assert llm.get_model("ainxt") == "gpt-4o"


@pytest.mark.parametrize("mode,expected", [("anthropic", True), ("openai", False), ("", False)])
def test_ainxt_uses_anthropic_flag(monkeypatch, mode, expected):
    monkeypatch.setattr(settings, "ainxt_compat_mode", mode)
    assert llm._ainxt_uses_anthropic() is expected


def test_ainxt_anthropic_client_builder_uses_auth_token():
    # Guards against SDK drift: the installed Anthropic SDK must accept auth_token=
    # + base_url= (Bearer auth for AiNxt's /v1/messages). Construction only — no call.
    client = llm._build_ainxt_anthropic("k")
    assert client is not None
