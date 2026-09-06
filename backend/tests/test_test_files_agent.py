# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the optional test-generation agent (SDLC Gap 7:
docs/ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3)."""
from app.agents.test_files import build_test_files
from app.config import settings


class TestBuildTestFiles:
    def test_returns_none_with_no_generated_files(self):
        assert build_test_files(plan_markdown="plan", generated_files=[]) is None

    def test_parses_single_file_block(self, monkeypatch):
        monkeypatch.setattr(
            "app.agents.test_files.call_llm",
            lambda **kw: "<<FILE: test_a.py>>\ndef test_x(): pass\n<<END>>",
        )
        out = build_test_files(
            plan_markdown="plan",
            generated_files=[{"path": "a.py", "content": "def x(): pass"}],
        )
        assert out == [{"path": "test_a.py", "content": "def test_x(): pass\n"}]

    def test_parses_multiple_file_blocks(self, monkeypatch):
        monkeypatch.setattr(
            "app.agents.test_files.call_llm",
            lambda **kw: (
                "<<FILE: test_a.py>>\ndef test_a(): pass\n<<END>>\n\n"
                "<<FILE: test_b.py>>\ndef test_b(): pass\n<<END>>"
            ),
        )
        out = build_test_files(
            plan_markdown="plan",
            generated_files=[{"path": "a.py", "content": "x"}, {"path": "b.py", "content": "y"}],
        )
        assert len(out) == 2
        assert {f["path"] for f in out} == {"test_a.py", "test_b.py"}

    def test_returns_none_on_llm_failure(self, monkeypatch):
        def _fail(**kw):
            raise RuntimeError("provider down")

        monkeypatch.setattr("app.agents.test_files.call_llm", _fail)
        out = build_test_files(
            plan_markdown="plan", generated_files=[{"path": "a.py", "content": "x"}],
        )
        assert out is None

    def test_returns_none_when_no_file_blocks_in_output(self, monkeypatch):
        monkeypatch.setattr("app.agents.test_files.call_llm", lambda **kw: "no blocks here")
        out = build_test_files(
            plan_markdown="plan", generated_files=[{"path": "a.py", "content": "x"}],
        )
        assert out is None

    def test_rejects_unsafe_paths_via_shared_parser(self, monkeypatch):
        # parse_files_from_output (shared with code_files.py) drops unsafe
        # paths — confirm that guard applies here too, not just to code_files.
        monkeypatch.setattr(
            "app.agents.test_files.call_llm",
            lambda **kw: "<<FILE: ../../etc/passwd>>\nmalicious\n<<END>>",
        )
        out = build_test_files(
            plan_markdown="plan", generated_files=[{"path": "a.py", "content": "x"}],
        )
        assert out is None

    def test_includes_code_context_when_provided(self, monkeypatch):
        captured = {}

        def _capture(*, system, messages, max_tokens, api_key=None):
            captured["user_msg"] = messages[0]["content"]
            return "<<FILE: test_a.py>>\nx\n<<END>>"

        monkeypatch.setattr("app.agents.test_files.call_llm", _capture)
        build_test_files(
            plan_markdown="plan",
            generated_files=[{"path": "a.py", "content": "x"}],
            code_context="existing test conventions here",
        )
        assert "existing test conventions here" in captured["user_msg"]

    def test_omits_code_context_section_when_absent(self, monkeypatch):
        captured = {}

        def _capture(*, system, messages, max_tokens, api_key=None):
            captured["user_msg"] = messages[0]["content"]
            return "<<FILE: test_a.py>>\nx\n<<END>>"

        monkeypatch.setattr("app.agents.test_files.call_llm", _capture)
        build_test_files(plan_markdown="plan", generated_files=[{"path": "a.py", "content": "x"}])
        assert "Repository conventions" not in captured["user_msg"]


class TestFeatureFlagDefault:
    def test_disabled_by_default(self):
        assert settings.enable_test_generation is False
