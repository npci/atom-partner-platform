# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared builder for the two review agents (code_reviewer + security_reviewer).

Both run the identical shape — generated files in, strict-JSON findings out — and
differ only in their system prompt + lens label, so the LLM call, validation, and
mock-fallback live here once. Mirrors the design/code/test agents' pattern
(strict JSON, _meta stamp, mock when no LLM key). No DB access.

Findings contract (per the review loop, "any finding blocks"):
  { "summary": str,
    "findings": [ { "severity": critical|high|medium|low|info,
                    "category": str, "file": str, "line": int|null,
                    "title": str, "detail": str, "suggested_fix": str } ] }
"""
import logging
import time

from app.core.llm import call_llm, get_model, get_provider

from ._common import (
    build_meta,
    extract_json,
    llm_key_present,
    read_partner_profile,
    runtime_override_key,
)

logger = logging.getLogger(__name__)

VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}

# A reviewer is two back-to-back large LLM calls (code then security); the second
# occasionally hits a transient provider error (overload / dropped connection).
# Retry so one blip doesn't abort the whole review / auto-fix loop.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_S = 2

# Bound the prompt: per-file cap + total cap, so a large change set can't blow
# the context window. The generated files are the primary input.
MAX_FILE_CHARS = 16000
MAX_TOTAL_CHARS = 90000


def _files_block(files: list[dict]) -> str:
    """Concatenate the generated files for the prompt, bounded. Each file is
    headed by its path so the agent can cite file:line."""
    parts: list[str] = []
    total = 0
    for f in files or []:
        path = f.get("path") or "?"
        body = (f.get("content") or "")[:MAX_FILE_CHARS]
        block = f"### FILE: {path}\n```\n{body}\n```"
        if total + len(block) > MAX_TOTAL_CHARS:
            parts.append(f"\n[... {len(files) - len(parts)} more file(s) omitted for length ...]")
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


def sanitize_findings(obj):
    """Coerce the model output into a clean findings dict, dropping individual
    malformed findings rather than rejecting the whole review (one odd finding
    must not abort the loop). Returns the cleaned dict, or None if the output
    isn't a usable findings object (no dict / no findings list — which we treat
    as invalid, NOT as 'clean', so broken code can't slip through).

    SDLC Gap 4 (docs/ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3): each finding is
    given `root_cause` and `principle_ref` fields (defaulted to None when the
    model didn't populate them — see the reviewer prompts' instructions to
    fill these in) so a finding carries a causal explanation and a citation
    back to the governing principle it violates, not just a symptom
    description. Never REJECTS a finding for lacking these — that would
    regress the "one odd finding must not abort the loop" guarantee above;
    they are additive, best-effort enrichment."""
    if not isinstance(obj, dict):
        return None
    raw = obj.get("findings")
    if not isinstance(raw, list):
        return None
    clean = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        if not (f.get("title") or f.get("detail")):
            continue  # nothing actionable
        sev = (f.get("severity") or "").strip().lower()
        f["severity"] = sev if sev in VALID_SEVERITIES else "medium"
        f.setdefault("root_cause", None)
        f.setdefault("principle_ref", None)
        clean.append(f)
    obj["findings"] = clean
    return obj


def mock_findings(lens: str) -> dict:
    """Schema-valid empty result when no LLM key is configured — keeps the loop
    runnable in a keyless demo (a review with no findings reads as 'clean')."""
    _, frontmatter = read_partner_profile()
    out = {
        "summary": f"[mock] no LLM key configured — {lens} review skipped (0 findings).",
        "findings": [],
    }
    out["_meta"] = build_meta(frontmatter, f"{get_provider()}:mock", mock=True)
    out["_meta"]["lens"] = lens
    return out


def build_review(
    *,
    lens: str,
    system_prompt: str,
    files: list[dict],
    change_title: str = "",
    design_summary: str | None = None,
    documents: list[dict] | None = None,
    code_context: str = "",
    api_key: str | None = None,
    max_tokens: int = 24000,
) -> dict | None:
    """Run one reviewer over the generated files. Retries on a transient LLM
    error or unparseable output (the most common cause of a spurious 'review
    failed' on the second back-to-back call). Returns the findings dict, or None
    only after exhausting attempts, so the caller raises and the agent_runs row
    records 'failed'."""
    if not files:
        return None

    raw_profile, frontmatter = read_partner_profile()

    sections = [f"# Partner profile (for context)\n{raw_profile}" if raw_profile else ""]
    if change_title:
        sections.append(f"# Change\n{change_title}")
    if design_summary:
        sections.append(f"# Design posture\n{design_summary}")
    for d in (documents or [])[:6]:
        dt = d.get("doc_type") or "doc"
        sections.append(f"# NPCI {dt}\n{(d.get('content') or '')[:6000]}")
    if code_context:
        sections.append(f"# Existing repository excerpts (for grounding)\n{code_context}")
    sections.append(
        "# Generated files to review\n"
        "Review ONLY these generated files. Cite findings by file path + line.\n\n"
        + _files_block(files)
    )
    user_msg = "\n\n".join(s for s in sections if s)

    last_reason = "unknown"
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            text = call_llm(
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=max_tokens,
                api_key=runtime_override_key(api_key),
            )
        except Exception as e:  # noqa: BLE001 — transient provider/truncation error
            last_reason = f"LLM error: {type(e).__name__}"  # type only — CWE-209
            logger.warning("%s review attempt %d/%d failed", lens, attempt, _MAX_ATTEMPTS, exc_info=True)
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_S * attempt)
            continue

        obj = sanitize_findings(extract_json(text))
        if obj is not None:
            obj["_meta"] = build_meta(frontmatter, f"{get_provider()}:{get_model()}")
            obj["_meta"]["lens"] = lens
            if attempt > 1:
                logger.info("%s review succeeded on attempt %d", lens, attempt)
            return obj

        last_reason = "non-JSON / unparseable output"
        logger.warning("%s review attempt %d/%d: %s", lens, attempt, _MAX_ATTEMPTS, last_reason)
        if attempt < _MAX_ATTEMPTS:
            time.sleep(_RETRY_BACKOFF_S * attempt)

    logger.error("%s review failed after %d attempts (%s)", lens, _MAX_ATTEMPTS, last_reason)
    return None
