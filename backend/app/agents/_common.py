# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Shared building blocks for the in-process partner agents (feasibility,
design, code, testing).

These are the bits every agent needs identically — reading PARTNER.md, bounding
the assembled prompt, tolerantly parsing the model's JSON, checking that a usable
LLM key exists, and stamping the `_meta` audit block. The feasibility agent grew
its own private copies first (app/agents/feasibility_analyser.py); this module is
the single home so design/code/testing don't each re-copy them (the plan commits
to three more agents reusing this — the §2.2 "wait for the third instance" bar is
met).

No DB writes. The one DB *read* is `read_partner_profile()`, which pulls the
UI-configured profile from the active `partner_profiles` row via its own
short-lived session and falls back to the mounted file — so agents stay
unit-testable with stub documents and a stub profile path (an empty DB → the
file path, unchanged).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings as _settings
from app.core.llm import get_provider

logger = logging.getLogger(__name__)

# Bounds — keep the assembled prompt comfortably under ~30K tokens. PARTNER.md
# is the long pole; per-doc caps mirror the feasibility analyser so prompt
# construction stays consistent across partner-side LLM features.
MAX_PROFILE_CHARS = 25000
MAX_DOC_CHARS = 12000
MAX_TOTAL_DOC_CHARS = 50000


def parse_frontmatter(raw: str) -> dict:
    """Parse the simple `key: value` block between the leading `---` fences
    (partner, profile_version, generated_at, …). Empty dict when absent."""
    fm: dict[str, str] = {}
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end > 0:
            for line in raw[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
    return fm


def _read_profile_from_db() -> str | None:
    """Return the active partner profile markdown from the DB, or None.

    Best-effort: opens its own short-lived session so the agents stay
    decoupled from the request's DB handle (base.Agent keeps DB objects out of
    `run()`). Any failure (table missing on first boot, no row, DB down) returns
    None so the caller falls back to the mounted file.
    """
    try:
        from app.database import SessionLocal
        from app.models import PartnerProfile
        with SessionLocal() as db:
            row = db.query(PartnerProfile).first()
            if row and row.content and row.content.strip():
                return row.content
    except Exception as e:  # noqa: BLE001 — fall back to the file
        logger.debug("partner profile DB read failed, using file", exc_info=True)
    return None


def read_partner_profile() -> tuple[str, dict]:
    """Return (raw_text, frontmatter). Truncated to MAX_PROFILE_CHARS.

    Source of truth is the active `partner_profiles` DB row (UI-configurable);
    the file at `settings.partner_profile_path` is the seed/fallback used when no
    row exists yet (fresh deploy, or unit tests that stub the path with an empty
    DB). Frontmatter is the flat `key: value` block between the leading `---`
    fences.
    """
    raw = _read_profile_from_db()
    if raw is None:
        path = Path(_settings.partner_profile_path)
        if not path.exists():
            return "", {}
        raw = path.read_text(encoding="utf-8")
    fm = parse_frontmatter(raw)
    if len(raw) > MAX_PROFILE_CHARS:
        raw = raw[:MAX_PROFILE_CHARS] + "\n\n... (truncated — exceeds MAX_PROFILE_CHARS)"
    return raw, fm


def truncate_change_docs(documents: list[dict]) -> str:
    """Concatenate change documents with per-doc + total caps. Each doc is a
    `{doc_type, content}` dict; same shape feasibility/question_suggester use."""
    parts: list[str] = []
    total = 0
    for d in documents:
        content = (d.get("content") or "").strip()
        if not content:
            continue
        chunk = content[:MAX_DOC_CHARS]
        piece = f"### {d.get('doc_type', 'document')}\n{chunk}"
        if total + len(piece) > MAX_TOTAL_DOC_CHARS:
            remaining = MAX_TOTAL_DOC_CHARS - total
            if remaining > 500:
                parts.append(piece[:remaining])
            break
        parts.append(piece)
        total += len(piece)
    return "\n\n".join(parts)


def extract_json(text: str) -> dict | None:
    """Best-effort JSON-object extraction from an LLM reply.

    Handles the three common Claude failure modes: plain JSON, markdown-fenced
    JSON (```json ... ```), and a prose preamble before the object.
    """
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl > 0:
            s = s[nl + 1:]
    if s.rstrip().endswith("```"):
        s = s.rstrip()[:-3].rstrip()
    start = s.find("{")
    end = s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None


def llm_key_present(api_key: str | None) -> bool:
    """True if a usable key exists for the active provider — a runtime override
    (Settings UI) or the env-level per-provider key."""
    if api_key:
        return True
    provider = get_provider()
    if provider == "openai":
        return bool(_settings.partner_openai_api_key)
    if provider == "ainxt":
        return bool(_settings.partner_ainxt_api_key)
    return bool(_settings.partner_anthropic_api_key)


def runtime_override_key(api_key: str | None) -> str | None:
    """Resolve the one-shot key to pass to call_llm: the supplied override for
    claude, else None (call_llm falls back to the env key). Returns None and the
    caller should skip when no key exists at all (use llm_key_present first)."""
    provider = get_provider()
    if provider == "claude" and api_key:
        return api_key
    return None


def build_meta(frontmatter: dict, model_used: str, *, mock: bool = False) -> dict:
    """The `_meta` audit block stamped onto every agent report.

    `tokens_used` (Finding 4: security_architecture_skills.md §4.2, EA_Skills.md
    P6/P10) reflects the most recent call_llm() invocation made by THIS agent
    run — read from core.llm's per-context usage tracker. 0 for the mock path
    (no LLM call happened) or a provider that didn't report usage.
    """
    tokens_used = 0
    if not mock:
        from app.core.llm import last_call_tokens
        tokens_used = last_call_tokens()
    return {
        "mock": mock,
        "profile_version": frontmatter.get("profile_version", "unknown"),
        "profile_generated_at": frontmatter.get("generated_at"),
        "model_used": model_used,
        "tokens_used": tokens_used,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
