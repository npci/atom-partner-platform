# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""LLM-driven per-TC test data suggester.

When the partner declares "Ready for Certification", the authority's cert-agent
runs the authority-initiated test cases against a partner simulator that needs
real-looking partner-side values (addressing handles, account identifiers, etc.).
Filling 20-50 test cases by hand is brutal, so this agent suggests
defaults derived from the change docs + the TC's scenario summary.

Provider switches via `LLM_PROVIDER` env (anthropic / openai / ainxt) —
same routing layer as `question_suggester`.

Output shape (strict JSON):
  {
    "test_data": {
      "payer_vpa":      "test1@partnerx",
      "payee_vpa":      "merchant@sim",
      "amount":         "100.00",
      "currency":       "INR",
      "ifsc":           "BKID0001234",
      "account_number": "ACCT-0001",
      "account_type":   "SAVINGS",
      "mobile_number":  "9999900001",
      "remarks":        "Cert run"
    },
    "rationale": "Short one-liner explaining the choices."
  }

The partner UI surfaces these as pre-filled form values and tags the
row with `ai_suggested=True`; the partner user can edit any field before
hitting Save (which clears `ai_suggested`).
"""
from __future__ import annotations

import json
import logging

from app.config import settings as _settings
from app.core.domain import get_active_pack
from app.core.llm import call_llm, get_model, get_provider

logger = logging.getLogger(__name__)


def _system_prompt() -> str:
    """Build the planner's system prompt from the active domain pack.

    A FUNCTION, not a module constant, for two reasons. It has to read the pack,
    and a module-level constant would freeze whichever pack happened to be
    active at import — invisible in production, actively wrong in tests that
    switch packs. It is also cheap: the registry caches the parse.

    The FIELD NAMES come from the pack's roles, so they are the same identifiers
    the Declare Ready dialog collects and the sign-off PDF prints — one
    definition, three consumers. Only the value-picking heuristics come from a
    prompt block, because those are genuine domain knowledge ("insufficient
    balance" means nothing to a library) and cannot be derived.
    """
    pack = get_active_pack()
    keys: list[str] = []
    for role in pack.roles:
        for f in role.fields:
            if f.key not in keys:
                keys.append(f.key)
    # Always-useful keys that are not role identifiers.
    for extra in ("remarks",):
        if extra not in keys:
            keys.append(extra)
    known = ", ".join(keys) if keys else "whatever the change documents imply"

    guidance = pack.prompt_block("test_data_guidance").strip()
    guidance_block = f"{guidance}\n" if guidance else (
        "- Derive values from the change documents; do not invent domain rules.\n"
    )

    return (
        f"You are the test data planner for {pack.prompt_block('partner_descriptor', 'an ecosystem partner')}. "
        f"Your job is to\nproduce realistic test_data values for a single "
        f"{pack.prompt_block('authority', 'authority')}-initiated certification test case\n"
        f"that will be executed against the partner's simulator.\n"
        "\n"
        "Rules:\n"
        "- Return STRICT JSON only. No prose, no markdown fences. Shape:\n"
        '    { "test_data": { ... }, "rationale": "one-line reason" }\n'
        "- Pick fields that are RELEVANT for the scenario; omit keys that don't apply.\n"
        f"  Common keys: {known}.\n"
        f"{guidance_block}"
        "- `remarks` should be a short tag tying back to the TC ID.\n"
        "- `rationale` is one sentence — what scenario keyword drove the picks.\n"
    )


def _truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " …"


def _extract_json(text: str) -> dict:
    """Best-effort JSON extraction — same approach as question_suggester."""
    if not text:
        return {}
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def suggest_test_data(
    api_key: str | None,
    change_title: str,
    tc_id: str,
    scenario_summary: str,
    expected_status: str = "",
    response_code: str = "",
    docs_context: str = "",
) -> dict:
    """Returns `{"test_data": {...}, "rationale": "..."}` or empty dict on
    any failure. Caller treats empty dict as "no suggestion" — the form
    stays blank for hand-entry.

    `docs_context` is an optional concatenated snippet from the change
    docs (BRD / cert_test_cases / product_note) to give the LLM partner-
    specific signals like routing codes or addressing handles.
    """
    provider = get_provider()

    runtime_override: str | None = None
    if provider == "anthropic":
        if api_key:
            runtime_override = api_key
        elif not _settings.partner_anthropic_api_key:
            logger.warning("suggest_test_data: no Anthropic key — skipping")
            return {}
    elif provider == "openai" and not _settings.partner_openai_api_key:
        logger.warning("suggest_test_data: no OpenAI key — skipping")
        return {}
    elif provider == "ainxt" and not _settings.partner_ainxt_api_key:
        logger.warning("suggest_test_data: no AiNxt key — skipping")
        return {}

    user_msg = (
        f"Change title: {change_title}\n"
        f"Test case ID: {tc_id}\n"
        f"Scenario:    {scenario_summary or '(no summary provided)'}\n"
        f"Expected status: {expected_status or '(unknown)'}\n"
        f"Response code:   {response_code or '(unknown)'}\n"
    )
    if docs_context:
        user_msg += (
            "\n--- Relevant change document excerpt ---\n"
            f"{_truncate(docs_context, 8000)}\n"
        )
    user_msg += "\nProduce the JSON per the rules."

    try:
        text = call_llm(
            system=_system_prompt(),
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=600,
            api_key=runtime_override,
        )
    except Exception as e:
        logger.error("suggest_test_data: LLM call failed (provider=%s tc=%s)", provider, tc_id, exc_info=True)
        return {}

    obj = _extract_json(text)
    test_data = obj.get("test_data") if isinstance(obj.get("test_data"), dict) else {}
    rationale = str(obj.get("rationale") or "")
    # Strip empty values — UI overlays the form so an empty key is noise
    test_data = {k: v for k, v in test_data.items() if v not in (None, "")}
    logger.info(
        "suggest_test_data: provider=%s model=%s tc=%s keys=%d",
        provider, get_model(), tc_id, len(test_data),
    )
    return {"test_data": test_data, "rationale": rationale}
