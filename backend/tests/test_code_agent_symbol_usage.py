# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the code agent's symbol-usage context wiring (SDLC Gap 2)."""
from app.agents.code import build_code_plan


class TestBuildCodePlanSymbolUsageContext:
    def test_symbol_usage_context_appears_in_prompt_when_grounded(self, monkeypatch):
        captured = {}

        def _fake_call_llm(*, system, messages, max_tokens, api_key=None):
            captured["user_msg"] = messages[0]["content"]
            return (
                '{"one_line_summary": "x", "code_posture": "plan_ready", '
                '"plan_markdown": "body", "work_items": []}'
            )

        monkeypatch.setattr("app.agents.code.call_llm", _fake_call_llm)
        monkeypatch.setattr(
            "app.agents._common.read_partner_profile",
            lambda: ("PARTNER.md content", {}),
        )

        result = build_code_plan(
            change_title="X",
            change_initial_prompt=None,
            change_enhanced_prompt=None,
            documents=[{"doc_type": "brd", "content": "y"}],
            code_context="some repo excerpt",
            symbol_usage_context="Cross-module symbol usage:\n- `PaymentRouter` referenced in: a.java",
        )
        assert result is not None
        assert "PaymentRouter" in captured["user_msg"]
        assert "Cross-module symbol usage" in captured["user_msg"]

    def test_no_symbol_usage_section_when_context_is_empty(self, monkeypatch):
        captured = {}

        def _fake_call_llm(*, system, messages, max_tokens, api_key=None):
            captured["user_msg"] = messages[0]["content"]
            return (
                '{"one_line_summary": "x", "code_posture": "plan_ready", '
                '"plan_markdown": "body", "work_items": []}'
            )

        monkeypatch.setattr("app.agents.code.call_llm", _fake_call_llm)
        monkeypatch.setattr(
            "app.agents._common.read_partner_profile",
            lambda: ("PARTNER.md content", {}),
        )

        build_code_plan(
            change_title="X",
            change_initial_prompt=None,
            change_enhanced_prompt=None,
            documents=[{"doc_type": "brd", "content": "y"}],
            symbol_usage_context=None,
        )
        assert "Cross-module symbol usage" not in captured["user_msg"]

    def test_none_symbol_usage_context_does_not_error(self, monkeypatch):
        monkeypatch.setattr(
            "app.agents.code.call_llm",
            lambda **kw: '{"one_line_summary": "x", "code_posture": "plan_ready", "plan_markdown": "b", "work_items": []}',
        )
        monkeypatch.setattr(
            "app.agents._common.read_partner_profile",
            lambda: ("PARTNER.md content", {}),
        )
        result = build_code_plan(
            change_title="X", change_initial_prompt=None, change_enhanced_prompt=None,
            documents=[{"doc_type": "brd", "content": "y"}],
        )
        assert result is not None
