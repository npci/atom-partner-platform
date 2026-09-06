# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CodeAgent — produces a partner-side implementation plan + change skeleton from
the design document + NPCI tech spec + PARTNER.md, optionally grounded in the
partner's own indexed source code (Code RAG, Phase 3.2).

Two postures, decided per-run by whether `code_context` (retrieved repo excerpts)
is supplied:
- **Grounded** (repo indexed): real file paths + symbols from the excerpts,
  higher confidence. `_meta.grounded=True`.
- **Spec-grounded fallback** (no repo / not indexed): best-guess paths with a
  `confidence` flag, code that needs the real repo surfaced as open questions.
  `_meta.grounded=False`.

Mirrors the design/testing agents: thin `Agent` wrapper (mock without a key) over
a pure `build_code_plan()`. Shared plumbing lives in `app/agents/_common.py`.

Still pending (Phase 3.4): whole-file generation + MR push (see backend
code_change.py for the NPCI reference)."""
from __future__ import annotations

import logging

from app.agents import _common
from app.agents.base import Agent
from app.agents.prompts import load_prompt
from app.core.llm import call_llm, get_model, get_provider

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = load_prompt("code.md")

POSTURES = {"plan_ready", "needs_repo_context", "risky", "blocked"}


def _validate(obj: dict) -> tuple[bool, str]:
    if not isinstance(obj, dict):
        return False, "code plan is not an object"
    for k in ("one_line_summary", "code_posture", "plan_markdown", "work_items"):
        if k not in obj:
            return False, f"missing top-level key: {k}"
    if obj["code_posture"] not in POSTURES:
        return False, f"invalid code_posture: {obj['code_posture']!r}"
    if not isinstance(obj["plan_markdown"], str) or not obj["plan_markdown"].strip():
        return False, "plan_markdown must be a non-empty string"
    if not isinstance(obj["work_items"], list):
        return False, "work_items must be a list"
    return True, ""


def build_code_plan(
    *,
    change_title: str,
    change_initial_prompt: str | None,
    change_enhanced_prompt: str | None,
    documents: list[dict],
    design_summary: str | None = None,
    revision_summary: str | None = None,
    knowledge_context: str | None = None,
    code_context: str | None = None,
    symbol_usage_context: str | None = None,
    api_key: str | None = None,
) -> dict | None:
    """Run the implementation-planning LLM call. Returns the plan dict, or None
    on failure. Pure — no DB writes.
    `knowledge_context` is a retrieved partner-KB block (Document RAG).
    `code_context` is retrieved source-code excerpts from the partner's indexed
    repo (Code RAG); when present the plan is repository-*grounded* — real paths +
    symbols — and `_meta.grounded` is set True. When absent the agent falls back
    to the spec-grounded skeleton (best-guess paths, low confidence).
    `symbol_usage_context` (SDLC Gap 2) is an advisory, pre-formatted block
    listing where the design's touched components are already referenced
    elsewhere in the repository (see rag/symbol_usage.py) — informs
    `file_changes[].confidence` and raises `open_questions` for symbols used
    outside the currently-planned file set."""
    profile_raw, frontmatter = _common.read_partner_profile()
    if not profile_raw:
        logger.warning("build_code_plan: partner profile missing")
        return None
    if not documents:
        logger.warning("build_code_plan: no change documents supplied")
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
    if design_summary:
        parts += ["", "---", "", f"Design posture (implement against this): {design_summary}"]
    parts += ["", "---", "", "NPCI change documents (baseline — version 1):", "", doc_block]
    if revision_summary:
        parts += [
            "", "---", "",
            "Changes introduced in later kit versions (plan against the current "
            "state — baseline plus these changes):", "", revision_summary,
        ]
    if knowledge_context:
        parts += [
            "", "---", "",
            "Relevant prior knowledge (retrieved from the partner knowledge base "
            "— specs, circulars, past implementations, standards). Use where applicable:",
            "", knowledge_context,
        ]
    grounded = bool(code_context and code_context.strip())
    if grounded:
        parts += [
            "", "---", "",
            "Relevant excerpts from the partner's OWN source repository (retrieved "
            "from the indexed codebase). Each is headed by its real file path. Ground "
            "your plan in these — reference the REAL paths, classes and methods you "
            "see here, set file_changes to actual paths, and raise confidence "
            "accordingly:", "", code_context,
        ]
        parts += [
            "",
            "You have repository excerpts above — produce a repository-grounded "
            "implementation plan JSON per the schema in the system prompt. Use real "
            "identifiers from the excerpts; only guess (low confidence) for code not "
            "shown.",
        ]
    else:
        parts += [
            "",
            "Remember: you do NOT have the partner's source code. Produce the "
            "implementation plan JSON per the schema in the system prompt.",
        ]
    if symbol_usage_context and symbol_usage_context.strip():
        parts += [
            "", "---", "",
            symbol_usage_context,
            "",
            "Before marking any file_changes entry 'high' confidence, check whether "
            "its symbol appears above with usages OUTSIDE the files you are already "
            "planning to change. If so, either include those files in file_changes "
            "or raise an open_question naming the untouched consumer — a symbol "
            "used elsewhere that your plan doesn't account for is a backward-"
            "compatibility risk, not something to guess past.",
        ]
    user_msg = "\n".join(parts)

    try:
        text = call_llm(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            # Plan markdown + per-file skeletons can be very large; use the model's
            # 64k ceiling  since 32k truncated on big changes.
            # call_llm raises on truncation.
            max_tokens=64000,
            api_key=_common.runtime_override_key(api_key),
        )
    except Exception as e:
        if "truncated at max_tokens" in str(e):
            raise RuntimeError(
                "the generated implementation plan exceeded the 64k output limit "
                "— the change is unusually large; re-run or split it."
            ) from e
        logger.error("build_code_plan: LLM call failed", exc_info=True)
        return None

    obj = _common.extract_json(text)
    if obj is None:
        logger.error(
            "build_code_plan: LLM output had no valid JSON; len=%d head=%r tail=%r",
            len(text), text[:600], text[-400:],
        )
        return None

    ok, err = _validate(obj)
    if not ok:
        logger.error("build_code_plan: shape invalid: %s; head=%r", err, str(obj)[:200])
        return None

    obj["_meta"] = _common.build_meta(frontmatter, f"{get_provider()}:{get_model()}")
    # True when retrieved repo excerpts were supplied (Code RAG); False = the
    # spec-grounded skeleton fallback.
    obj["_meta"]["grounded"] = grounded
    logger.info(
        "build_code_plan: provider=%s model=%s docs=%d posture=%s grounded=%s",
        get_provider(), get_model(), len(documents), obj.get("code_posture"), grounded,
    )
    return obj


def _mock_plan(reason: str) -> dict:
    body = (
        "## Implementation plan\n_Mock plan — configure an LLM key to generate the "
        f"real plan._\n\n(Reason: {reason}.)\n"
    )
    return {
        "one_line_summary": "Mock implementation plan — configure an LLM key to run the real code agent.",
        "code_posture": "needs_repo_context",
        "plan_markdown": body,
        "work_items": [],
        "file_changes": [],
        "dependencies": [],
        "risks": [],
        "open_questions": [],
        "_meta": {"mock": True, "grounded": False, "profile_version": "unknown", "model_used": f"{get_provider()}:mock"},
    }


class CodeAgent(Agent):
    """Author a spec-grounded implementation plan + change skeleton (MVP)."""

    def run(self, input: dict) -> dict:
        api_key = input.get("api_key")
        if not _common.llm_key_present(api_key):
            logger.info("CodeAgent: no LLM key — returning mock plan")
            return _mock_plan("no LLM key configured")

        plan = build_code_plan(
            change_title=input.get("change_title", "Untitled Change"),
            change_initial_prompt=input.get("change_initial_prompt"),
            change_enhanced_prompt=input.get("change_enhanced_prompt"),
            documents=input.get("documents") or [],
            design_summary=input.get("design_summary"),
            revision_summary=input.get("revision_summary"),
            knowledge_context=input.get("knowledge_context"),
            code_context=input.get("code_context"),
            symbol_usage_context=input.get("symbol_usage_context"),
            api_key=api_key,
        )
        if plan is None:
            raise RuntimeError(
                "code agent produced no plan — check logs (profile missing, no "
                "documents, or model returned non-JSON)."
            )
        return plan
