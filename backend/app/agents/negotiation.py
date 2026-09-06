# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""NegotiationAgent — STUB. Returns mock output demonstrating the I/O shape.

Replace the `run()` body (and `prompts/negotiation.md`) with your real drafting
logic, or host your own and repoint the manifest at a `url:`. Do not change the
wiring. See prompts/negotiation.md for the I/O shape. (The in-platform
`question_suggester` covers draft-query suggestions today; folding it into this
agent is a planned fast-follow.)
"""
from __future__ import annotations

from app.agents.base import Agent
from app.agents.prompts import load_prompt


class NegotiationAgent(Agent):
    def run(self, input: dict) -> dict:
        _ = load_prompt(self.prompt or "negotiation.md")
        topic = input.get("topic", "the proposed terms")
        kind = input.get("kind", "query")
        return {
            "agent": "negotiation",
            "status": "mock",
            "kind": kind,
            "subject": f"[mock] {topic}",
            "draft_message": (
                f"[mock] Draft {kind} regarding {topic} — replace NegotiationAgent.run() "
                "with your real drafting logic."
            ),
            "rationale": "[mock] Rationale grounded in the partner profile goes here.",
        }
