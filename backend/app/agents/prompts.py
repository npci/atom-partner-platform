"""Prompt loader for agents — reads `app/agents/prompts/<name>` (cached) and
optionally interpolates variables.

Mirrors the NPCI `excel_testcase_engine` loader (cached file read) and adds
interpolation via `string.Template`. Template substitutes `$name` / `${name}`
and **leaves literal `{` `}` braces untouched** — important because prompts
embed JSON examples that would break `str.format`. Missing variables are left
as-is (`safe_substitute`), so a partial var set never raises.

    SYSTEM_PROMPT = load_prompt("feasibility.md")            # no vars → raw text
    msg = load_prompt("design.md", change_title="UPI Lite")   # $change_title filled
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@lru_cache(maxsize=None)
def _read(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def load_prompt(name: str, **variables) -> str:
    """Return the prompt file `name`. With variables, interpolate `$var`/`${var}`
    via `Template.safe_substitute` (braces in the text are preserved)."""
    text = _read(name)
    if not variables:
        return text
    return Template(text).safe_substitute(**variables)


def clear_cache() -> None:
    """Test/dev hook — drop the cached file reads so edits are picked up."""
    _read.cache_clear()
