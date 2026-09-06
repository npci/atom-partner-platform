# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the post-convergence design-alignment check (SDLC Gap 6:
docs/ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3)."""
from app.agents.design_alignment import check_alignment


class TestCheckAlignment:
    def test_returns_skipped_shape_with_no_files(self):
        out = check_alignment("some plan", [])
        assert out["aligned"] is None
        assert out["deviations"] == []

    def test_returns_aligned_true_when_model_says_so(self, monkeypatch):
        monkeypatch.setattr(
            "app.agents.design_alignment.call_llm",
            lambda **kw: '{"aligned": true, "deviations": []}',
        )
        out = check_alignment("plan", [{"path": "a.py"}])
        assert out["aligned"] is True
        assert out["deviations"] == []

    def test_returns_deviations_when_model_finds_them(self, monkeypatch):
        monkeypatch.setattr(
            "app.agents.design_alignment.call_llm",
            lambda **kw: '{"aligned": false, "deviations": ["missing file X", "wrong signature on Y"]}',
        )
        out = check_alignment("plan", [{"path": "a.py"}])
        assert out["aligned"] is False
        assert len(out["deviations"]) == 2

    def test_returns_none_on_llm_failure(self, monkeypatch):
        def _fail(**kw):
            raise RuntimeError("provider down")

        monkeypatch.setattr("app.agents.design_alignment.call_llm", _fail)
        out = check_alignment("plan", [{"path": "a.py"}])
        assert out["aligned"] is None
        assert out["deviations"] == []

    def test_returns_none_on_unparseable_output(self, monkeypatch):
        monkeypatch.setattr(
            "app.agents.design_alignment.call_llm",
            lambda **kw: "not json at all",
        )
        out = check_alignment("plan", [{"path": "a.py"}])
        assert out["aligned"] is None

    def test_returns_none_when_output_missing_aligned_key(self, monkeypatch):
        monkeypatch.setattr(
            "app.agents.design_alignment.call_llm",
            lambda **kw: '{"something_else": true}',
        )
        out = check_alignment("plan", [{"path": "a.py"}])
        assert out["aligned"] is None

    def test_non_list_deviations_defaults_to_empty(self, monkeypatch):
        monkeypatch.setattr(
            "app.agents.design_alignment.call_llm",
            lambda **kw: '{"aligned": false, "deviations": "not a list"}',
        )
        out = check_alignment("plan", [{"path": "a.py"}])
        assert out["deviations"] == []

    def test_truncates_long_plan_markdown(self, monkeypatch):
        captured = {}

        def _capture(*, system, messages, max_tokens, **kw):
            captured["user_msg"] = messages[0]["content"]
            return '{"aligned": true, "deviations": []}'

        monkeypatch.setattr("app.agents.design_alignment.call_llm", _capture)
        huge_plan = "x" * 50_000
        check_alignment(huge_plan, [{"path": "a.py"}])
        # The plan section of the prompt must be bounded, not the full 50k chars.
        assert len(captured["user_msg"]) < 20_000
