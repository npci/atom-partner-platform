# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""TestAgent — produces a partner-side test plan for an incoming NPCI change,
grounded in PARTNER.md + the change's product-kit documents (incl. the NPCI
`cert_test_cases`) + the design document.

The partner does NOT execute the authoritative certification suite — that runs on
NPCI's cert orchestrator after the partner declares readiness. So this agent
AUTHORS the plan and maps coverage to the NPCI cert cases (the delegation
bridge); execution stays in the existing cert lifecycle.

Module is `testing.py` (not `test.py`) to avoid pytest discovery / stdlib `test`
collision; the registry NAME is still "test".

Mirrors the design agent: thin `Agent` wrapper (mock degradation) over a pure
`build_test_plan()`. Shared plumbing lives in `app/agents/_common.py`.
"""
from __future__ import annotations

import logging

from app.agents import _common
from app.agents.base import Agent
from app.agents.prompts import load_prompt
from app.core.llm import call_llm, get_model, get_provider

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = load_prompt("test.md")

READINESS = {"ready_to_certify", "needs_test_data", "gaps", "blocked"}


def _validate(obj: dict) -> tuple[bool, str]:
    if not isinstance(obj, dict):
        return False, "test plan is not an object"
    for k in ("one_line_summary", "readiness", "test_plan_markdown", "suites"):
        if k not in obj:
            return False, f"missing top-level key: {k}"
    if obj["readiness"] not in READINESS:
        return False, f"invalid readiness: {obj['readiness']!r}"
    if not isinstance(obj["test_plan_markdown"], str) or not obj["test_plan_markdown"].strip():
        return False, "test_plan_markdown must be a non-empty string"
    if not isinstance(obj["suites"], list):
        return False, "suites must be a list"
    for s in obj["suites"]:
        if not isinstance(s, dict) or "suite" not in s:
            return False, f"invalid suite entry: {s!r}"
    return True, ""


def build_test_plan(
    *,
    change_title: str,
    change_initial_prompt: str | None,
    change_enhanced_prompt: str | None,
    documents: list[dict],
    design_summary: str | None = None,
    revision_summary: str | None = None,
    knowledge_context: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Run the test-planning LLM call. Returns the plan dict.

    Raises RuntimeError naming the specific cause on failure — the message is
    surfaced verbatim to the operator by the dashboard endpoint, so it must say
    which of the failure modes fired rather than listing all of them.

    Pure — no DB writes. `knowledge_context` is a retrieved partner-KB block
    (Document RAG) added alongside the full current-change documents."""
    profile_raw, frontmatter = _common.read_partner_profile()
    if not profile_raw:
        raise RuntimeError(
            "the partner profile is empty — there is no active partner_profiles "
            "row and no readable file at PARTNER_PROFILE_PATH. Upload a profile "
            "via Settings, or mount PARTNER.md."
        )
    if not documents:
        raise RuntimeError(
            "the change has no documents — NPCI has not published a product kit "
            "for it yet, so there is nothing to plan tests against."
        )

    doc_block = _common.truncate_change_docs(documents)
    if not doc_block:
        raise RuntimeError(
            f"all {len(documents)} change document(s) are empty — the rows exist "
            "but none carries content. Check the v1 baseline rows in "
            "change_documents; the plan is built from v1, not the latest version."
        )

    parts: list[str] = [
        "PARTNER.md", "", profile_raw, "", "---", "",
        f"NPCI proposed change: {change_title}",
    ]
    if change_initial_prompt:
        parts.append(f"\nPO prompt: {change_initial_prompt}")
    if change_enhanced_prompt:
        parts.append(f"\nEnhanced prompt: {change_enhanced_prompt}")
    if design_summary:
        parts += ["", "---", "", f"Design posture (test against this): {design_summary}"]
    parts += [
        "", "---", "",
        "NPCI change documents (baseline — version 1; includes cert_test_cases "
        "when present):", "", doc_block,
    ]
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
            "— specs, circulars, past test plans, standards). Use where applicable:",
            "", knowledge_context,
        ]
    parts += ["", "Produce the test plan JSON per the schema in the system prompt."]
    user_msg = "\n".join(parts)

    try:
        text = call_llm(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            # Test plan markdown + per-suite cases is the largest single-JSON
            # output of the analyse agents; a real plan truncated at 32k, so use
            # the model's full 64k ceiling . call_llm raises on truncation.
            max_tokens=64000,
            api_key=_common.runtime_override_key(api_key),
        )
    except Exception as e:
        # Truncation is a distinct, actionable failure — propagate the clear
        # message instead of returning None (which the caller reports as the
        # misleading "profile missing / no documents / non-JSON").
        if "truncated at max_tokens" in str(e):
            raise RuntimeError(
                "the generated test plan exceeded the 64k output limit — the "
                "change is unusually large; re-run or split it."
            ) from e
        logger.error("build_test_plan: LLM call failed", exc_info=True)
        # Type only — the provider's message can echo the request URL, model
        # id and key prefix, and this string lands on the job row that the
        # dashboard renders (CWE-209). Full detail is in the log line above.
        raise RuntimeError(f"the LLM call failed ({type(e).__name__})") from e

    obj = _common.extract_json(text)
    if obj is None:
        logger.error(
            "build_test_plan: LLM output had no valid JSON; len=%d head=%r tail=%r",
            len(text), text[:600], text[-400:],
        )
        raise RuntimeError(
            f"the model returned no valid JSON ({len(text)} chars). "
            f"Output began: {text[:200]!r}"
        )

    ok, err = _validate(obj)
    if not ok:
        logger.error("build_test_plan: shape invalid: %s; head=%r", err, str(obj)[:200])
        raise RuntimeError(f"the model returned JSON of the wrong shape — {err}")

    obj["_meta"] = _common.build_meta(frontmatter, f"{get_provider()}:{get_model()}")
    logger.info(
        "build_test_plan: provider=%s model=%s docs=%d readiness=%s",
        get_provider(), get_model(), len(documents), obj.get("readiness"),
    )
    return obj


def _mock_plan(reason: str) -> dict:
    body = (
        "## Test plan\n_Mock test plan — configure an LLM key to generate the real "
        f"plan._\n\n(Reason: {reason}.)\n"
    )
    return {
        "one_line_summary": "Mock test plan — configure an LLM key to run the real test agent.",
        "readiness": "gaps",
        "test_plan_markdown": body,
        "suites": [{
            "suite": "functional", "summary": "Mock output — no LLM key configured.",
            "cases": [],
        }],
        "cert_coverage": {"npci_cases_total": 0, "covered": 0, "gaps": [f"Mock: {reason}."]},
        "test_data_needed": [],
        "open_questions": [],
        "_meta": {"mock": True, "profile_version": "unknown", "model_used": f"{get_provider()}:mock"},
    }


class TestAgent(Agent):
    """Author a partner-side test plan + cert-coverage mapping for a change."""

    def run(self, input: dict) -> dict:
        api_key = input.get("api_key")
        if not _common.llm_key_present(api_key):
            logger.info("TestAgent: no LLM key — returning mock test plan")
            return _mock_plan("no LLM key configured")

        return build_test_plan(
            change_title=input.get("change_title", "Untitled Change"),
            change_initial_prompt=input.get("change_initial_prompt"),
            change_enhanced_prompt=input.get("change_enhanced_prompt"),
            documents=input.get("documents") or [],
            design_summary=input.get("design_summary"),
            revision_summary=input.get("revision_summary"),
            knowledge_context=input.get("knowledge_context"),
            api_key=api_key,
        )
