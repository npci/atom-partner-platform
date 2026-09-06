# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Code-quality reviewer agent — one of the two lenses in the review step.

Reviews the generated whole-file change set for correctness, bugs, and needless
complexity (NOT security — that's security_reviewer). Emits the shared findings
JSON. Mock (0 findings) when no LLM key, like the other agents.
"""
from . import review_base
from .base import Agent
from .prompts import load_prompt

SYSTEM_PROMPT = load_prompt("code_reviewer.md")

LENS = "code_quality"


class CodeReviewerAgent(Agent):
    def run(self, input: dict) -> dict:
        api_key = input.get("api_key")
        if not review_base.llm_key_present(api_key):
            return review_base.mock_findings(LENS)
        out = review_base.build_review(
            lens=LENS,
            system_prompt=SYSTEM_PROMPT,
            files=input.get("files") or [],
            change_title=input.get("change_title") or "",
            design_summary=input.get("design_summary"),
            documents=input.get("documents") or [],
            code_context=input.get("code_context") or "",
            api_key=api_key,
        )
        if out is None:
            raise RuntimeError("code review failed (no key / LLM error / invalid output)")
        return out
