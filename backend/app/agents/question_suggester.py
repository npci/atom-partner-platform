# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""LLM-driven clarification question suggester.

Reads the Product Kit documents shared by NPCI for a given change and asks the
configured LLM to produce 3-7 specific clarification questions a UPI ecosystem
partner should ask NPCI before implementing the change.

Provider switches via `LLM_PROVIDER` env (claude / openai / ainxt) — see
`app.core.llm` for the routing layer. Pre-Slice-llm-routing this module
called Anthropic directly; now it goes through `call_llm` so the partner
stack picks up OpenAI or the AiNxt internal gateway when configured.
"""
import json
import logging

from app.config import settings as _settings
from app.core.llm import call_llm, get_model, get_provider

logger = logging.getLogger(__name__)

MAX_DOC_CHARS = 15000  # per-document cap to keep context bounded
MAX_TOTAL_CHARS = 60000  # hard cap across all docs


SYSTEM_PROMPT = """You are the technical lead of a UPI ecosystem partner (a bank or PSP) reviewing
change documentation shared by NPCI. Your job is to produce a short list of clarification
questions the partner should ask NPCI BEFORE starting implementation.

Focus on items a partner genuinely needs answered to build safely and on time:
- API / integration contracts (endpoint semantics, field formats, error codes, retry policy)
- Edge cases and failure modes (timeouts, partial failures, rollback, idempotency)
- Operational parameters (limits, thresholds, go-live dates, sunset of prior behaviour)
- Compliance / regulatory implications (RBI, KYC, consent, audit trail)
- Cross-system dependencies (PSPs, banks, issuer/acquirer roles)
- Ambiguous or under-specified statements in the shared documents

Rules:
- Return STRICT JSON only. No prose, no markdown fences.
- Shape: {"questions": ["Question 1?", "Question 2?", ...]}
- 3 to 7 questions total. Fewer is fine if docs are clear.
- Each question must be complete, specific, and end with "?".
- Reference concrete items from the docs where possible ("Section 4.2 mentions...", "The manifest shows...").
- Avoid generic filler ("Any more details?", "Please clarify.").
- Do not ask questions already answered by the shared documents.
"""


def _truncate_docs(documents: list[dict]) -> str:
    """Concatenate documents with per-doc and total caps."""
    parts: list[str] = []
    total = 0
    for d in documents:
        content = (d.get("content") or "").strip()
        if not content:
            continue
        chunk = content[:MAX_DOC_CHARS]
        header = f"### {d.get('doc_type', 'document')}"
        piece = f"{header}\n{chunk}"
        if total + len(piece) > MAX_TOTAL_CHARS:
            remaining = MAX_TOTAL_CHARS - total
            if remaining > 500:
                parts.append(piece[:remaining])
            break
        parts.append(piece)
        total += len(piece)
    return "\n\n".join(parts)


def _extract_questions(text: str) -> list[str]:
    """Best-effort JSON extraction. Tolerates surrounding whitespace / stray chars."""
    if not text:
        return []
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    questions = obj.get("questions", [])
    if not isinstance(questions, list):
        return []
    out: list[str] = []
    for q in questions:
        if isinstance(q, str):
            s = q.strip()
            if s:
                out.append(s)
    return out


def suggest_questions(
    api_key: str | None,
    change_title: str,
    documents: list[dict],
    revision_summary: str | None = None,
) -> list[str]:
    """Generate clarification questions. Returns [] on any failure —
    caller decides what to do.

    `api_key` is retained for back-compat with the existing call site
    (which reads `partner_anthropic_api_key` from `partner_settings`).
    It's only used today as a "is the partner allowed to use AI?"
    gate when the configured provider is `claude` AND the env-level
    key is empty — covers the dev flow where a per-partner key is
    pasted in the Settings UI rather than baked into the env.
    For openai/ainxt providers it's ignored; those keys come from env.
    """
    provider = get_provider()

    # Key resolution mirrors the existing dashboard flow: when the
    # caller passed an `api_key` (read from `partner_settings` /
    # settings.partner_anthropic_api_key) AND the active provider is
    # claude, override for this single call. For openai/ainxt the
    # env-level key is the source today (extend the Settings UI to
    # store those if/when runtime override is needed). When neither
    # the runtime override nor env key is configured for the active
    # provider, bail with a warn rather than letting the cached
    # client builder raise downstream.
    runtime_override: str | None = None
    if provider == "claude":
        if api_key:
            runtime_override = api_key
        elif not _settings.partner_anthropic_api_key:
            logger.warning("suggest_questions: no Anthropic key — skipping")
            return []
    elif provider == "openai":
        if not _settings.partner_openai_api_key:
            logger.warning("suggest_questions: no OpenAI key — skipping")
            return []
    elif provider == "ainxt":
        if not _settings.partner_ainxt_api_key:
            logger.warning("suggest_questions: no AiNxt key — skipping")
            return []

    if not documents:
        return []

    doc_block = _truncate_docs(documents)
    if not doc_block:
        return []

    revision_block = (
        f"Changes introduced in later kit versions (factor these into the questions):\n\n{revision_summary}\n\n"
        if revision_summary else ""
    )
    user_msg = (
        f"Change title: {change_title}\n\n"
        f"Shared documents (baseline — version 1):\n\n{doc_block}\n\n"
        f"{revision_block}"
        "Produce the JSON per the rules."
    )

    try:
        text = call_llm(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=1500,
            api_key=runtime_override,
        )
    except Exception as e:
        logger.error("suggest_questions: LLM call failed (provider=%s)", provider, exc_info=True)
        return []

    questions = _extract_questions(text)
    logger.info(
        "suggest_questions: provider=%s model=%s docs=%d chars=%d -> %d questions",
        provider, get_model(), len(documents), len(doc_block), len(questions),
    )
    return questions
