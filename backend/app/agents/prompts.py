# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Prompt loader for agents — reads `app/agents/prompts/<name>` (cached) and
optionally interpolates variables.

Mirrors the NPCI `excel_testcase_engine` loader (cached file read) and adds
interpolation via `string.Template`. Template substitutes `$name` / `${name}`
and **leaves literal `{` `}` braces untouched** — important because prompts
embed JSON examples that would break `str.format`. Missing variables are left
as-is (`safe_substitute`), so a partial var set never raises.

    SYSTEM_PROMPT = load_prompt("feasibility.md")            # no vars → raw text
    msg = load_prompt("design.md", change_title="UPI Lite")   # $change_title filled

Principle preamble (SDLC Gap 1: neither the code-generation nor the review
prompts fed the platform's own architecture/security standards as a governing
input — see docs/ARCHITECTURE_REVIEW_ACTIONS.md). Every generation- and
review-facing prompt in `_PREAMBLE_PROMPTS` is prefixed with
`_principles_preamble.md` (derived from EA_Skills.md /
security_architecture_skills.md) before its own content, giving every agent a
deterministic priority order and an explicit anti-pattern checklist to work
against. Prompts NOT in that set (e.g. `negotiation.md`, which drafts partner
correspondence and neither generates nor reviews code) are left unprefixed —
the preamble's content (secrets, timeouts, exception handling, N+1 queries) is
not relevant to their task and would just consume prompt budget.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# Prompts that generate or review code/specifications and must be governed by
# the platform's architecture/security principles. Deliberately an explicit
# allowlist (not "everything except negotiation.md") so a future prompt is
# safe-by-default (no preamble) unless someone consciously opts it in.
_PREAMBLE_PROMPTS: frozenset[str] = frozenset({
    "code.md", "code_files.md", "design.md", "code_reviewer.md", "security_reviewer.md",
})


@lru_cache(maxsize=None)
def _read(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def load_prompt(name: str, **variables) -> str:
    """Return the prompt file `name`, prefixed with the governing-principles
    preamble when `name` is in `_PREAMBLE_PROMPTS`. With variables, interpolate
    `$var`/`${var}` via `Template.safe_substitute` (braces in the text are
    preserved) — applied to the COMBINED text so a prompt that itself uses
    `$var` placeholders still resolves correctly with the preamble attached."""
    text = _read(name)
    if name in _PREAMBLE_PROMPTS:
        text = f"{_read('_principles_preamble.md')}\n\n---\n\n{text}"
    if not variables:
        return text
    return Template(text).safe_substitute(**variables)


def clear_cache() -> None:
    """Test/dev hook — drop the cached file reads so edits are picked up."""
    _read.cache_clear()
