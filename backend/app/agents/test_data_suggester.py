# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""LLM-driven per-TC test data suggester.

When the partner declares "Ready for Certification", NPCI's cert-agent
runs the NPCI-initiated test cases against a bank simulator that needs
real-looking bank-side values (payer VPA, account number, IFSC, etc.).
Filling 20-50 test cases by hand is brutal, so this agent suggests
defaults derived from the change docs + the TC's scenario summary.

Provider switches via `LLM_PROVIDER` env (claude / openai / ainxt) —
same routing layer as `question_suggester`.

Output shape (strict JSON):
  {
    "test_data": {
      "payer_vpa":      "test1@bankx",
      "payee_vpa":      "merchant@npcisim",
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
row with `ai_suggested=True`; the bank user can edit any field before
hitting Save (which clears `ai_suggested`).
"""
from __future__ import annotations

import json
import logging

from app.config import settings as _settings
from app.core.llm import call_llm, get_model, get_provider

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the test data planner for a UPI ecosystem bank. Your job is to
produce realistic test_data values for a single NPCI-initiated certification test case
that will be executed against the bank's simulator.

Rules:
- Return STRICT JSON only. No prose, no markdown fences. Shape:
    { "test_data": { ... }, "rationale": "one-line reason" }
- Pick fields that are RELEVANT for the scenario; omit keys that don't apply.
  Common keys: payer_vpa, payee_vpa, amount, currency, ifsc, account_number,
  account_type, mobile_number, remarks, merchant_category_code, original_txn_id.
- Use the bank's known artifacts (IFSC, account prefixes, VPA handle) when
  the change docs reveal them. If unknown, fall back to safe defaults:
  - VPAs:   "test1@bankx" (payer), "merchant@npcisim" (payee)
  - IFSC:   "BKID0001234"
  - Amount: derive from scenario keywords
     - "insufficient" / "low balance"      → "999999.00"
     - "limit exceeded"                    → "200000.00"
     - "partial capture" / "partial"       → "500.00"
     - normal / success / deemed           → "100.00"
  - currency: always "INR"
  - account_type: "SAVINGS" unless scenario says current
- `remarks` should be a short tag tying back to the TC ID.
- `rationale` is one sentence — what scenario keyword drove the picks.
"""


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
    docs (BRD / cert_test_cases / product_note) to give the LLM bank-
    specific signals like IFSC codes or VPA handles.
    """
    provider = get_provider()

    runtime_override: str | None = None
    if provider == "claude":
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
            system=SYSTEM_PROMPT,
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
