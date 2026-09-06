# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""DesignAgent — produces a partner-side design document for an incoming NPCI
change, grounded in PARTNER.md + the change's product-kit documents.

Mirrors the feasibility agent: a thin `Agent` wrapper (mock degradation when no
LLM key) over a pure `build_design()` function that does the LLM call + JSON
parse + shape validation. Shared plumbing (profile read, JSON extraction, key
check, _meta) lives in `app/agents/_common.py`.

A bank customises by editing `build_design`/`prompts/design.md`, or hosts its own
service and repoints the manifest at a `url:`.
"""
from __future__ import annotations

import logging

from app.agents import _common
from app.agents.base import Agent
from app.agents.prompts import load_prompt
from app.core.llm import call_llm, get_model, get_provider

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = load_prompt("design.md")

POSTURES = {"ready", "needs_review", "risky", "blocked"}


def _validate(obj: dict) -> tuple[bool, str]:
    """Cheap shape check — strict enough to catch a hallucinated shape, loose
    enough to let the model legitimately leave lists empty."""
    if not isinstance(obj, dict):
        return False, "design is not an object"
    for k in ("one_line_summary", "design_posture", "document_markdown", "sections"):
        if k not in obj:
            return False, f"missing top-level key: {k}"
    if obj["design_posture"] not in POSTURES:
        return False, f"invalid design_posture: {obj['design_posture']!r}"
    if not isinstance(obj["document_markdown"], str) or not obj["document_markdown"].strip():
        return False, "document_markdown must be a non-empty string"
    if not isinstance(obj["sections"], list):
        return False, "sections must be a list"
    for s in obj["sections"]:
        if not isinstance(s, dict) or "section" not in s:
            return False, f"invalid section entry: {s!r}"
    return True, ""


def build_design(
    *,
    change_title: str,
    change_initial_prompt: str | None,
    change_enhanced_prompt: str | None,
    documents: list[dict],
    feasibility_summary: str | None = None,
    revision_summary: str | None = None,
    knowledge_context: str | None = None,
    api_key: str | None = None,
) -> dict | None:
    """Run the design LLM call. Returns the design dict on success, None on
    failure (missing profile, no docs, bad/empty JSON). Pure — no DB writes.

    `knowledge_context`, when supplied, is a retrieved block from the partner
    knowledge base (Document RAG) — prior knowledge to ground the design,
    alongside the full current-change documents."""
    profile_raw, frontmatter = _common.read_partner_profile()
    if not profile_raw:
        logger.warning("build_design: partner profile missing")
        return None
    if not documents:
        logger.warning("build_design: no change documents supplied")
        return None

    doc_block = _common.truncate_change_docs(documents)
    if not doc_block:
        return None

    parts: list[str] = [
        "PARTNER.md", "", profile_raw, "", "---", "",
        f"NPCI proposed change: {change_title}",
    ]
    if change_initial_prompt:
        parts.append(f"\nPO prompt: {change_initial_prompt}")
    if change_enhanced_prompt:
        parts.append(f"\nEnhanced prompt: {change_enhanced_prompt}")
    if feasibility_summary:
        parts += ["", "---", "", f"Feasibility posture (build on this): {feasibility_summary}"]
    parts += ["", "---", "", "NPCI change documents (baseline — version 1):", "", doc_block]
    if revision_summary:
        parts += [
            "", "---", "",
            "Changes introduced in later kit versions (design against the current "
            "state — baseline plus these changes):", "", revision_summary,
        ]
    if knowledge_context:
        parts += [
            "", "---", "",
            "Relevant prior knowledge (retrieved from the partner knowledge base "
            "— specs, circulars, past changes, standards). Use where applicable:",
            "", knowledge_context,
        ]
    parts += ["", "Produce the design document JSON per the schema in the system prompt."]
    user_msg = "\n".join(parts)

    try:
        text = call_llm(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            # The full design document (markdown body + structured index) can be
            # very large; use the model's 64k ceiling  since 32k
            # truncated on big changes. call_llm raises on truncation.
            max_tokens=64000,
            api_key=_common.runtime_override_key(api_key),
        )
    except Exception as e:
        if "truncated at max_tokens" in str(e):
            raise RuntimeError(
                "the generated design document exceeded the 64k output limit — "
                "the change is unusually large; re-run or split it."
            ) from e
        logger.error("build_design: LLM call failed", exc_info=True)
        return None

    obj = _common.extract_json(text)
    if obj is None:
        logger.error(
            "build_design: LLM output had no valid JSON; len=%d head=%r tail=%r",
            len(text), text[:600], text[-400:],
        )
        return None

    ok, err = _validate(obj)
    if not ok:
        logger.error("build_design: shape invalid: %s; head=%r", err, str(obj)[:200])
        return None

    obj["_meta"] = _common.build_meta(frontmatter, f"{get_provider()}:{get_model()}")
    logger.info(
        "build_design: provider=%s model=%s docs=%d posture=%s",
        get_provider(), get_model(), len(documents), obj.get("design_posture"),
    )
    return obj


def _mock_design(reason: str) -> dict:
    """Schema-valid mock so a key-less clone still produces output (marked
    `_meta.mock=True`)."""
    body = (
        "## Overview\n_Mock design document — configure an LLM key to generate the "
        f"real design._\n\n(Reason: {reason}.)\n"
    )
    return {
        "one_line_summary": "Mock design document — configure an LLM key to run the real design agent.",
        "design_posture": "needs_review",
        "document_markdown": body,
        "sections": [{
            "section": "overview", "status": "unknown",
            "summary": "Mock output — no LLM key configured.",
            "details": [f"Mock design agent: {reason}."],
        }],
        "components_touched": [],
        "dependencies": [],
        "risks": [],
        "open_questions": [],
        "_meta": {"mock": True, "profile_version": "unknown", "model_used": f"{get_provider()}:mock"},
    }


class DesignAgent(Agent):
    """Produce a partner-side design document for an incoming NPCI change."""

    def run(self, input: dict) -> dict:
        api_key = input.get("api_key")
        if not _common.llm_key_present(api_key):
            logger.info("DesignAgent: no LLM key — returning mock design")
            return _mock_design("no LLM key configured")

        design = build_design(
            change_title=input.get("change_title", "Untitled Change"),
            change_initial_prompt=input.get("change_initial_prompt"),
            change_enhanced_prompt=input.get("change_enhanced_prompt"),
            documents=input.get("documents") or [],
            feasibility_summary=input.get("feasibility_summary"),
            revision_summary=input.get("revision_summary"),
            knowledge_context=input.get("knowledge_context"),
            api_key=api_key,
        )
        if design is None:
            raise RuntimeError(
                "design agent produced no document — check logs (profile missing, "
                "no documents, or model returned non-JSON)."
            )
        return design
