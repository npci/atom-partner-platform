# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Post-convergence design-alignment check (SDLC Gap 6:
docs/ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3).

Runs once after the review/fix loop reaches 'clean' (zero findings from the
code-quality, security, and lint lenses). Non-blocking / informational by
design: "clean" today means zero review findings, which is NOT the same
claim as "implements what the design document said" — no phase of the
pipeline previously checked generated-code-against-design-intent at all.

Deliberately NOT a hard gate: retrofitting a blocking check onto an
already-shipped review loop risks false-positive lockout (an LLM-graded
alignment judgment is inherently less reliable than the deterministic-plus-
LLM-reviewer combination that already gates the merge request). This module
surfaces a signal for the human MR reviewer instead of silently trusting
'clean' to also mean 'matches intent'.
"""
from __future__ import annotations

import logging

from app.core.llm import call_llm

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You compare an implementation plan against the files that
were actually generated. Report ONLY meaningful deviations — file changes the
plan described that are missing from the generated set, or generated files
that do something materially different from what the plan described. Ignore
cosmetic differences (variable names, comment wording, import ordering).
Respond with ONE JSON object and nothing else (no prose, no markdown fence):
{"aligned": true|false, "deviations": ["<short description>", ...]}"""

_MAX_PLAN_CHARS = 12000


def check_alignment(plan_markdown: str, files: list[dict]) -> dict:
    """Returns {"aligned": bool|None, "deviations": [str, ...]}. `aligned` is
    None (not True/False) when the check itself could not run (no LLM key,
    provider error, unparseable output) — callers must treat None as
    "no signal available," not as a failure to align. Never raises."""
    if not files:
        return {"aligned": None, "deviations": [], "_meta": {"skipped": True, "reason": "no files"}}

    file_list = "\n".join(f"- {f.get('path', '?')}" for f in files)
    user_msg = (
        f"# Implementation plan\n{(plan_markdown or '')[:_MAX_PLAN_CHARS]}\n\n"
        f"# Files actually generated\n{file_list}"
    )
    try:
        text = call_llm(
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=2000,
        )
        from app.agents._common import extract_json
        obj = extract_json(text)
        if isinstance(obj, dict) and "aligned" in obj:
            deviations = obj.get("deviations")
            return {
                "aligned": obj.get("aligned"),
                "deviations": deviations if isinstance(deviations, list) else [],
            }
        logger.warning("design_alignment: unparseable/unusable LLM output")
    except Exception:  # noqa: BLE001 — non-fatal, this check is advisory only
        logger.warning("design_alignment check failed (non-fatal)", exc_info=True)

    return {"aligned": None, "deviations": [], "_meta": {"skipped": True}}
