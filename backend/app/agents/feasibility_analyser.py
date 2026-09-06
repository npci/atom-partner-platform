# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Feasibility analyser — produces a structured per-area assessment of an
incoming NPCI change against the partner's PARTNER.md capability profile.

Single-call LLM with strict JSON output. The output is persisted by the
caller; this module stays pure (no DB writes) so it can be unit-tested with
stub documents and a stub profile path.

The system prompt now lives in `app/agents/prompts/feasibility.md` and is read
via the prompt loader (WS3) — edit the prompt there, not here. The 6 areas
mirror what partners can negotiate on (production_deadline, scope, limits,
technical_spec, upstream_dependencies, certification_role) so the analyser's
output plugs directly into the counter-drafting / query flows on the partner UI.

`FeasibilityAgent` (app/agents/feasibility.py) wraps `analyse_change` as the
shipped in-process agent; the manifest can repoint feasibility at a remote
service without touching this module.
"""
import json
import logging
from datetime import datetime, timezone

from app.agents import _common
from app.agents.prompts import load_prompt
from app.config import settings as _settings
from app.core.llm import call_llm, get_model, get_provider

logger = logging.getLogger(__name__)

# Bounds — keep the assembled prompt comfortably under 30K tokens for any
# reasonable model. The PARTNER.md cap lives in `_common.MAX_PROFILE_CHARS`
# (the profile read moved there); per-doc caps mirror question_suggester to keep
# memory + cost predictable.
MAX_DOC_CHARS = 12000
MAX_TOTAL_DOC_CHARS = 50000

AREAS = [
    "production_deadline",
    "scope",
    "limits",
    "technical_spec",
    "upstream_dependencies",
    "certification_role",
]

# Externalised system prompt (WS3). load_prompt with no vars returns the raw
# text, so the JSON braces in the example are preserved untouched.
SYSTEM_PROMPT = load_prompt("feasibility.md")


def _read_partner_profile() -> tuple[str, dict]:
    """Returns (raw_text, frontmatter). DB-backed (active partner_profiles row)
    with file fallback — delegates to the shared loader so the analyser and the
    design/code/testing agents all read the same UI-configured profile."""
    return _common.read_partner_profile()


def _truncate_change_docs(documents: list[dict]) -> str:
    """Concatenate change documents with per-doc + total caps. Same shape as
    question_suggester._truncate_docs so prompt-construction stays consistent
    across partner-side LLM features."""
    parts: list[str] = []
    total = 0
    for d in documents:
        content = (d.get("content") or "").strip()
        if not content:
            continue
        chunk = content[:MAX_DOC_CHARS]
        header = f"### {d.get('doc_type', 'document')}"
        piece = f"{header}\n{chunk}"
        if total + len(piece) > MAX_TOTAL_DOC_CHARS:
            remaining = MAX_TOTAL_DOC_CHARS - total
            if remaining > 500:
                parts.append(piece[:remaining])
            break
        parts.append(piece)
        total += len(piece)
    return "\n\n".join(parts)


def _extract_json(text: str) -> dict | None:
    """Best-effort JSON extraction.

    Handles three common Claude failure modes:
      1. Plain JSON                — `{...}`
      2. Markdown-fenced JSON      — ```json\n{...}\n```  (Claude does this
         frequently despite prompt instructions not to)
      3. Prose preamble + JSON     — `Here is the report:\n{...}`
    """
    if not text:
        return None
    s = text.strip()
    # Strip leading code fence with optional language tag.
    if s.startswith("```"):
        nl = s.find("\n")
        if nl > 0:
            s = s[nl + 1:]
    # Strip trailing code fence.
    if s.rstrip().endswith("```"):
        s = s.rstrip()[:-3].rstrip()
    # Now isolate the outermost JSON object.
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None


def _validate_report(obj: dict) -> tuple[bool, str]:
    """Cheap shape validation — returns (ok, error_msg). Strict enough to
    catch hallucinated shapes; loose enough to allow the model to legitimately
    mark areas as out_of_scope or leave action lists empty."""
    if not isinstance(obj, dict):
        return False, "report is not an object"
    required = (
        "one_line_summary",
        "overall_posture",
        "areas",
        "internal_action_summary",
        "npci_communication_summary",
        "additional_findings",
    )
    for k in required:
        if k not in obj:
            return False, f"missing top-level key: {k}"
    if not isinstance(obj["areas"], list) or not obj["areas"]:
        return False, "areas must be a non-empty list"
    for a in obj["areas"]:
        if not isinstance(a, dict):
            return False, "area entry is not an object"
        if a.get("area") not in AREAS:
            return False, f"unknown area: {a.get('area')!r}"
    if not isinstance(obj["npci_communication_summary"], list):
        return False, "npci_communication_summary must be a list"
    for m in obj["npci_communication_summary"]:
        if not isinstance(m, dict) or not m.get("draft_message"):
            return False, f"invalid npci_communication entry: {m!r}"
    return True, ""


def analyse_change(
    *,
    change_title: str,
    change_initial_prompt: str | None,
    change_enhanced_prompt: str | None,
    documents: list[dict],
    revision_summary: str | None = None,
    api_key: str | None = None,
) -> dict | None:
    """Run the analyser. Returns the report dict on success, None on failure.

    Reads PARTNER.md from `settings.partner_profile_path` and the supplied
    change documents (each a `{doc_type, content}` dict). Persistence is
    the caller's job — keeps this module pure for tests.

    `api_key` mirrors `question_suggester` — when the active provider is
    claude AND a runtime override is supplied (from `partner_settings` /
    Settings UI), this call uses that key instead of the env-level one.
    """
    profile_raw, frontmatter = _read_partner_profile()
    if not profile_raw:
        logger.warning(
            "analyse_change: partner profile missing at %s",
            _settings.partner_profile_path,
        )
        return None
    if not documents:
        logger.warning("analyse_change: no change documents supplied")
        return None

    doc_block = _truncate_change_docs(documents)
    if not doc_block:
        return None

    provider = get_provider()
    runtime_override: str | None = None
    if provider == "claude":
        if api_key:
            runtime_override = api_key
        elif not _settings.partner_anthropic_api_key:
            logger.warning("analyse_change: no Anthropic key — skipping")
            return None
    elif provider == "openai":
        if not _settings.partner_openai_api_key:
            logger.warning("analyse_change: no OpenAI key — skipping")
            return None
    elif provider == "ainxt":
        if not _settings.partner_ainxt_api_key:
            logger.warning("analyse_change: no AiNxt key — skipping")
            return None

    user_msg_parts: list[str] = [
        "PARTNER.md",
        "",
        profile_raw,
        "",
        "---",
        "",
        f"NPCI proposed change: {change_title}",
    ]
    if change_initial_prompt:
        user_msg_parts.append(f"\nPO prompt: {change_initial_prompt}")
    if change_enhanced_prompt:
        user_msg_parts.append(f"\nEnhanced prompt: {change_enhanced_prompt}")
    user_msg_parts.extend([
        "",
        "---",
        "",
        "NPCI change documents (baseline — version 1):",
        "",
        doc_block,
    ])
    if revision_summary:
        user_msg_parts.extend([
            "",
            "---",
            "",
            "Changes introduced in later kit versions (analyse the current state — "
            "baseline plus these changes — against PARTNER.md):",
            "",
            revision_summary,
        ])
    user_msg_parts.extend([
        "",
        "Produce the JSON report per the schema in the system prompt.",
    ])
    user_msg = "\n".join(user_msg_parts)

    try:
        text = call_llm(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            # Sized to the richest profile, not the smallest. 8000 fit HDFC's
            # ~3700-token report but a larger partner profile (e.g. SBI's
            # research-draft) produces a longer 6-area analysis that truncated
            # at 8000 → invalid JSON → 502. 16000 clears the observed ~8000-token
            # SBI output with headroom; Sonnet-4-6 allows up to 64K out, so
            # bump again in 4K steps if an even larger profile truncates.
            max_tokens=16000,
            api_key=runtime_override,
        )
    except Exception as e:
        logger.error("analyse_change: LLM call failed", exc_info=True)
        return None

    obj = _extract_json(text)
    if obj is None:
        # Log head + tail so truncation vs. trailing prose vs. invalid JSON
        # are all distinguishable from one line. Total log size bounded at
        # ~1500 chars regardless of LLM output length.
        logger.error(
            "analyse_change: LLM output did not contain valid JSON; "
            "len=%d head=%r tail=%r",
            len(text), text[:600], text[-400:],
        )
        return None

    ok, err = _validate_report(obj)
    if not ok:
        logger.error(
            "analyse_change: report shape invalid: %s; obj head=%r",
            err, str(obj)[:200],
        )
        return None

    obj["_meta"] = {
        "profile_version": frontmatter.get("profile_version", "unknown"),
        "profile_generated_at": frontmatter.get("generated_at"),
        "model_used": f"{provider}:{get_model()}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        "analyse_change: provider=%s model=%s docs=%d posture=%s",
        provider, get_model(), len(documents), obj.get("overall_posture"),
    )
    return obj
