# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""FeasibilityAgent — the shipped, working feasibility agent.

Wraps the real `analyse_change` logic so NPCI ships feasibility as runnable
code (not a stub). Degrades to a representative MOCK report when no LLM key is
configured, so a fresh clone produces output without credentials. The 6-area
output shape is identical to the legacy analyser path (acceptance criterion O5).

A bank customises by editing `run()` here + `prompts/feasibility.md`, or hosts
their own service and repoints the manifest at a `url:`.
"""
from __future__ import annotations

import logging

from app.agents.base import Agent
from app.agents.feasibility_analyser import AREAS, analyse_change
from app.config import settings
from app.core.llm import get_provider

logger = logging.getLogger(__name__)


def _llm_key_present(api_key: str | None) -> bool:
    """True if a usable key exists for the active provider (runtime override
    from Settings UI, or the env-level per-provider key)."""
    if api_key:
        return True
    provider = get_provider()
    if provider == "openai":
        return bool(settings.partner_openai_api_key)
    if provider == "ainxt":
        return bool(settings.partner_ainxt_api_key)
    return bool(settings.partner_anthropic_api_key)


def _mock_report(reason: str) -> dict:
    """A schema-valid mock so a key-less clean clone still produces output.
    Marked clearly (`_meta.mock=True`) so the UI/ops can tell it apart."""
    areas = [
        {
            "area": area,
            "status": "unknown",
            "summary": "Mock output — no LLM key configured.",
            "confidence": "low",
            "findings": [f"Mock feasibility agent: {reason}."],
            "internal_actions": [],
            "npci_communications": [],
            "referenced_profile_sections": [],
            "referenced_change_docs": [],
        }
        for area in AREAS
    ]
    return {
        "one_line_summary": "Mock feasibility report — configure an LLM key to run the real analysis.",
        "overall_posture": "ready_with_conditions",
        "areas": areas,
        "internal_action_summary": [],
        "npci_communication_summary": [],
        "additional_findings": [f"Mock output returned because: {reason}."],
        "_meta": {
            "mock": True,
            "profile_version": "unknown",
            "model_used": f"{get_provider()}:mock",
        },
    }


class FeasibilityAgent(Agent):
    """Assess whether the partner can implement an incoming NPCI change."""

    def run(self, input: dict) -> dict:
        api_key = input.get("api_key")
        if not _llm_key_present(api_key):
            logger.info("FeasibilityAgent: no LLM key — returning mock report")
            return _mock_report("no LLM key configured")

        report = analyse_change(
            change_title=input.get("change_title", "Untitled Change"),
            change_initial_prompt=input.get("change_initial_prompt"),
            change_enhanced_prompt=input.get("change_enhanced_prompt"),
            documents=input.get("documents") or [],
            revision_summary=input.get("revision_summary"),
            api_key=api_key,
        )
        if report is None:
            # A real failure (bad JSON, missing profile, etc.) — raise so the
            # audit row records status=failed instead of masking it as a mock.
            raise RuntimeError(
                "feasibility analyser produced no report — check logs "
                "(profile missing, no documents, or model returned non-JSON)."
            )
        return report
