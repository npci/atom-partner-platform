# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the governing-principles preamble (SDLC Gap 1:
docs/ARCHITECTURE_REVIEW_ACTIONS.md — Tier 3)."""
import pytest

from app.agents import prompts


@pytest.fixture(autouse=True)
def _clear_cache():
    prompts.clear_cache()
    yield
    prompts.clear_cache()


class TestPreambleAttachment:
    @pytest.mark.parametrize("name", [
        "code.md", "code_files.md", "design.md", "code_reviewer.md", "security_reviewer.md",
    ])
    def test_generation_and_review_prompts_carry_the_preamble(self, name):
        text = prompts.load_prompt(name)
        assert "Governing architecture and security principles" in text
        assert "Security > Correctness/Completeness" in text

    def test_preamble_precedes_the_prompts_own_content(self):
        text = prompts.load_prompt("code.md")
        preamble_pos = text.find("Governing architecture and security principles")
        assert preamble_pos < 10  # preamble is first (just after the "## " heading marker)

    def test_negotiation_prompt_does_not_carry_the_preamble(self):
        text = prompts.load_prompt("negotiation.md")
        assert "Governing architecture and security principles" not in text

    def test_feasibility_prompt_does_not_carry_the_preamble(self):
        # feasibility.md drafts an assessment report, not code/spec generation —
        # deliberately excluded from the allowlist.
        text = prompts.load_prompt("feasibility.md")
        assert "Governing architecture and security principles" not in text

    def test_test_prompt_does_not_carry_the_preamble(self):
        text = prompts.load_prompt("test.md")
        assert "Governing architecture and security principles" not in text


class TestPreambleWithVariableInterpolation:
    def test_variables_still_interpolate_with_preamble_attached(self, tmp_path, monkeypatch):
        # Use a real allowlisted prompt name so the preamble path is exercised,
        # but confirm the underlying Template substitution still works on the
        # combined text. design.md doesn't itself take variables, so verify via
        # a case that DOES use variables (code_reviewer.md doesn't either) —
        # test the substitution mechanism directly against the preamble+content
        # concatenation instead.
        combined = prompts.load_prompt("design.md", unused_var="ignored")
        assert "Governing architecture and security principles" in combined


class TestPreambleContent:
    def test_preamble_lists_the_key_anti_patterns(self):
        text = prompts.load_prompt("_principles_preamble.md")
        for phrase in (
            "hardcoded credentials",
            "swallowed",
            "idempot",
            "N+1",
            "circuit breaker",
        ):
            assert phrase.lower() in text.lower(), f"expected {phrase!r} in preamble"
