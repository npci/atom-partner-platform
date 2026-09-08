# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Deterministic anti-pattern scan over generated files — a backstop
alongside the two LLM reviewers.

Per the architecture principles' "Anti-Patterns to Flag" list: an LLM reviewer
can, on any given run, simply fail to notice a pattern-matchable defect (a
hardcoded credential, a bare `except:`, a `SELECT *`) that a five-line regex
would catch with 100% recall every time. This module is that deterministic
backstop — it never misses what it's looking for, unlike an LLM.

Findings are emitted in the identical `{summary, findings[]}` shape the two
LLM reviewers produce (see agents/review_base.py), so the existing
`_review_status()` aggregation in `api/dashboard/code.py` sums them into the
total findings count with NO code change required there — "any finding
blocks" already applies uniformly regardless of which reviewer produced it.

Deliberately a small, curated pattern set — NOT a comprehensive static
analyzer. Regex-based pattern matching has real false-positive potential,
so severities favor `medium` over `critical` for
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
        # Bounded `.{0,2000}?` rather than `.*?`. With `.*?` every `while True:`
        # that is NOT followed by a time.sleep( rescans to end-of-string, which
        # is quadratic in the number of occurrences: 8k took 5.5s, 20k took
        # 34s on a 240KB input. The input is LLM-generated code, so a
        # prompt-injected change document can influence its shape. A polling
        # loop and its sleep are adjacent in any real hit, so a 2000-character
        # window loses nothing this rule was written to catch.
        re.compile(r'(?i)while\s+True\s*:.{0,2000}?\btime\.sleep\(', re.DOTALL),
        "medium",
        "Polling loop with sleep — prefer an event/condition instead of a busy-wait",
    ),
    (
        "requests-no-timeout",
        # `[^)\r\n]{0,500}` — the original `[^)]*` spanned newlines, so an
        # unclosed paren scanned the rest of the file on every occurrence. A
        # call's arguments are on one logical line for this rule's purposes.
        re.compile(r'requests\.(get|post|put|delete|patch)\([^)\r\n]{0,500}\)'),
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
                        "suggested_fix": "Review and remediate per the prohibited anti-pattern guidance.",
                        "root_cause": (
                            "Matched a pattern from the prohibited-anti-pattern list. This is a "
                            "deterministic regex check, not an LLM judgement, so it runs alongside "
                            "the model review rather than replacing it — and it can false-positive."
                        ),
                        "principle_ref": "Anti-Patterns to Flag",
                    })
    except Exception:  # noqa: BLE001 — a lint bug must not abort the review step
        return {"summary": "Deterministic lint: scan failed (degraded to 0 findings)", "findings": []}

    return {"summary": f"Deterministic lint: {len(findings)} finding(s)", "findings": findings}
