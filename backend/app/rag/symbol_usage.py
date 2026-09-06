# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Lightweight cross-module usage check — NOT a full call-graph/AST engine.

SDLC Gap 2 (docs/ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3): the code agent's
plan is built from RAG-retrieved excerpts (semantic-similarity search over
`document_chunks`) but never checks "who else in the repository references the
symbols I am about to change." This module closes that gap pragmatically:
it greps the partner's indexed repository (via GitLab's own code-search API,
reusing the exact auth path `code_ingestion.py` already established) for
textual references to symbols named in the design report's
`components_touched[]`, so the code agent's plan is informed by WHERE ELSE
those symbols are used before it proposes changing them.

This is a deliberate middle ground: a real dependency-index/call-graph engine
(per the SDLC review's reference methodology — a full 7-column dependency
index, call-chain construction, hidden-dependency detection) is a
significantly larger investment than this platform's current scale
justifies. A GitLab code-search grep gives most of the practical value
(surfacing likely-affected files the design/plan didn't already know about)
at a fraction of the engineering cost, and is a natural stepping stone if a
full engine is built later.

Advisory only — never gates anything. A search failure (GitLab unreachable,
token missing, rate-limited) degrades to an empty result; the code agent
falls back to whatever it already knew from RAG retrieval.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import CodeRepo
from app.rag.code_ingestion import _gitlab_project, _gitlab_token

logger = logging.getLogger(__name__)

# Symbols shorter than this are almost always false-positive-prone (common
# words, single-letter loop variables that happen to match a component name)
# and would flood the plan prompt with noise rather than signal.
_MIN_SYMBOL_LEN = 3

# Cap total symbols searched per plan run — GitLab's code-search API is rate
# limited and this is a "nice to have" signal, not a blocking dependency; a
# design report with an unusually long components_touched[] must not turn one
# code-plan run into dozens of sequential API calls.
_MAX_SYMBOLS_SEARCHED = 20


def find_symbol_usages(
    db: Session, repo: CodeRepo, symbols: list[str], *, max_hits_per_symbol: int = 15,
) -> dict[str, list[str]]:
    """Returns {symbol: [file_path, ...]} — files whose content mentions the
    symbol name, via GitLab's own code-search API. Empty dict on any failure
    or when there is nothing usable to search (this is advisory context, not
    a gate — callers must treat an empty result as "no signal," not "no usages
    exist")."""
    token = _gitlab_token(db)
    if not token or not symbols:
        return {}
    try:
        project = _gitlab_project(repo, token)
    except Exception:  # noqa: BLE001 — advisory signal; degrade to empty
        logger.warning("symbol_usage: could not open project handle", exc_info=True)
        return {}

    # De-duplicate while preserving order, then cap — see _MAX_SYMBOLS_SEARCHED.
    seen: set[str] = set()
    candidates: list[str] = []
    for sym in symbols:
        s = (sym or "").strip()
        if not s or len(s) < _MIN_SYMBOL_LEN or s.lower() in seen:
            continue
        seen.add(s.lower())
        candidates.append(s)
    candidates = candidates[:_MAX_SYMBOLS_SEARCHED]

    out: dict[str, list[str]] = {}
    for sym in candidates:
        try:
            results = project.search("blobs", sym)
        except Exception:  # noqa: BLE001 — one symbol's search failure must not abort the rest
            logger.warning("symbol_usage: search failed for %r", sym, exc_info=True)
            continue
        paths: list[str] = []
        for r in (results or [])[:max_hits_per_symbol]:
            p = r.get("path") if isinstance(r, dict) else None
            if p and p not in paths:
                paths.append(p)
        if paths:
            out[sym] = paths
    return out


def format_usage_context(usage_map: dict[str, list[str]]) -> str:
    """Render `find_symbol_usages()`'s output as a plain-text block suitable
    for injection into the code-plan prompt. Empty string when there is
    nothing to show, so callers can unconditionally append it without an
    extra `if usage_map:` at every call site."""
    if not usage_map:
        return ""
    lines = ["Cross-module symbol usage (from a repository code search — advisory, not exhaustive):"]
    for sym, paths in usage_map.items():
        lines.append(f"- `{sym}` referenced in: {', '.join(paths)}")
    return "\n".join(lines)
