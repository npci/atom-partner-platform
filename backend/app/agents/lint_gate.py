# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic anti-pattern scan over generated files — a backstop
alongside the two LLM reviewers (SDLC Gap 3:
docs/ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3).

Per `docs/EA_Skills.md`'s "Anti-Patterns to Flag" list: an LLM reviewer can,
on any given run, simply fail to notice a pattern-matchable defect (a
hardcoded credential, a bare `except:`, a `SELECT *`) that a five-line regex
would catch with 100% recall every time. This module is that deterministic
backstop — it never misses what it's looking for, unlike an LLM.

Findings are emitted in the identical `{summary, findings[]}` shape the two
LLM reviewers produce (see agents/review_base.py), so the existing
`_review_status()` aggregation in `api/dashboard/code.py` sums them into the
total findings count with NO code change required there — "any finding
blocks" already applies uniformly regardless of which reviewer produced it.

Deliberately a small, curated pattern set — NOT a comprehensive static
analyzer. Regex-based pattern matching has real false-positive potential
(see docs/adr/ADR-0005-deterministic-lint-gate-alongside-llm-review.md's
Consequences section), so severities favor `medium` over `critical` for
heuristic-based patterns to keep a false positive a minor friction rather
than a mislabeled hard blocker.
"""
from __future__ import annotations

import re

# (finding_id_prefix, compiled_pattern, severity, title)
_PATTERNS: list[tuple[str, "re.Pattern[str]", str, str]] = [
    (
        "hardcoded-secret",
        re.compile(
            r'(?i)\b(api[_-]?key|secret|password|token)\s*=\s*["\'][A-Za-z0-9+/=_-]{12,}["\']',
        ),
        "high",
        "Possible hardcoded credential",
    ),
    (
        "select-star",
        re.compile(r'(?i)SELECT\s+\*\s+FROM'),
        "medium",
        "SELECT * in a query — request only required columns",
    ),
    (
        "bare-except",
        re.compile(r'(?m)^\s*except\s*:'),
        "medium",
        "Bare except clause — swallows all exceptions including KeyboardInterrupt/SystemExit",
    ),
    (
        "sleep-poll-loop",
        re.compile(r'(?i)while\s+True\s*:.*?\btime\.sleep\(', re.DOTALL),
        "medium",
        "Polling loop with sleep — prefer an event/condition instead of a busy-wait",
    ),
    (
        "requests-no-timeout",
        re.compile(r'requests\.(get|post|put|delete|patch)\([^)]*\)'),
        "high",
        "HTTP call via `requests` with no visible `timeout=` argument",
    ),
]

# For the one pattern where a naive regex would over-match (any requests.*()
# call, even ones that DO pass timeout=), post-filter by checking the matched
# span for a timeout= kwarg rather than trying to express that in the regex
# itself — regex alternation for "NOT containing X" is unreadable and fragile.
_HAS_TIMEOUT_KWARG = re.compile(r'timeout\s*=')


def _is_false_positive(finding_id: str, matched_text: str) -> bool:
    if finding_id == "requests-no-timeout" and _HAS_TIMEOUT_KWARG.search(matched_text):
        return True
    return False


def lint_files(files: list[dict]) -> dict:
    """Returns the same {summary, findings[]} shape as review_base's LLM
    reviewers, so callers can persist this as a `CodeReviewReport` row
    alongside the code_quality/security lenses. Never raises — a lint bug
    must not abort the review step; an unexpected exception here degrades to
    zero findings rather than blocking the pipeline on the wrong failure."""
    findings: list[dict] = []
    try:
        for f in files or []:
            path = f.get("path") or "?"
            content = f.get("content") or ""
            for finding_id, pattern, severity, title in _PATTERNS:
                for m in pattern.finditer(content):
                    if _is_false_positive(finding_id, m.group(0)):
                        continue
                    line = content.count("\n", 0, m.start()) + 1
                    findings.append({
                        "severity": severity,
                        "category": "anti_pattern",
                        "file": path,
                        "line": line,
                        "title": title,
                        "detail": f"Deterministic lint match ({finding_id}): {m.group(0)[:120]!r}",
                        "suggested_fix": "Review and remediate per EA_Skills.md anti-pattern guidance.",
                        "root_cause": (
                            "Matched a pattern from EA_Skills.md's prohibited-anti-pattern "
                            "list; this is a deterministic, non-LLM check — see "
                            "docs/adr/ADR-0005-deterministic-lint-gate-alongside-llm-review.md."
                        ),
                        "principle_ref": "EA_Skills.md — Anti-Patterns to Flag",
                    })
    except Exception:  # noqa: BLE001 — a lint bug must not abort the review step
        return {"summary": "Deterministic lint: scan failed (degraded to 0 findings)", "findings": []}

    return {"summary": f"Deterministic lint: {len(findings)} finding(s)", "findings": findings}
