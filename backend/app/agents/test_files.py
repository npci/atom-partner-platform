# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Optional test-generation agent — sibling to code_files.py (SDLC Gap 7:
docs/ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3).

Generates unit-test files for the code-review-loop's generated files, using
the same batched whole-file `<<FILE: path>> ... <<END>>` convention as
code_files.py (reuses its parser directly — one wire format, one place that
understands it).

DISABLED BY DEFAULT (`settings.enable_test_generation = False`). This is
explicitly supplementary, not a substitute for the partner's own CI/test
suite — see ARCHITECTURE.md's "Scope of the automated code-review gate"
section, which states plainly that the review/fix loop does not generate or
run tests. This module exists for partners who want automated test
scaffolding as an additional, optional signal, without changing the
platform's default (documented, honest) behavior.

Pure — no DB / no GitLab here, mirroring code_files.py's shape. The caller
persists/pushes the result if it chooses to.
"""
from __future__ import annotations

import logging

from app.agents import _common
from app.agents.code_files import parse_files_from_output
from app.agents.prompts import load_prompt
from app.core.llm import call_llm

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = load_prompt("test_files.md")

# Generated files are the primary input; bound per-file size so a handful of
# large files don't blow the prompt budget before the plan/context even fit.
_MAX_FILE_CHARS = 12000
_MAX_TOTAL_FILE_CHARS = 80000


def _files_block(files: list[dict]) -> str:
    parts: list[str] = []
    total = 0
    for f in files or []:
        path = f.get("path") or "?"
        body = (f.get("content") or "")[:_MAX_FILE_CHARS]
        block = f"<<FILE: {path}>>\n{body}\n<<END>>"
        if total + len(block) > _MAX_TOTAL_FILE_CHARS:
            parts.append(f"\n[... {len(files) - len(parts)} more file(s) omitted for length ...]")
            break
        parts.append(block)
        total += len(block)
    return "\n\n".join(parts)


def build_test_files(
    *,
    plan_markdown: str,
    generated_files: list[dict],
    code_context: str | None = None,
    api_key: str | None = None,
    max_tokens: int = 32000,
) -> list[dict] | None:
    """Generate test files for `generated_files`. Returns [{path, content}] or
    None on failure (no files produced, LLM error, or the output couldn't be
    parsed as `<<FILE>>` blocks). Pure — no DB / GitLab side effects."""
    if not generated_files:
        logger.warning("build_test_files: no generated files supplied")
        return None

    files_block = _files_block(generated_files)
    parts: list[str] = [
        f"Implementation plan:\n{(plan_markdown or '')[:8000]}",
        "",
        "Generated files to write tests for:",
        files_block,
    ]
    if code_context and code_context.strip():
        parts += [
            "",
            "Repository conventions (existing tests, for style matching):",
            code_context,
        ]
    parts.append(
        "\nEmit one or more test files as <<FILE: path>> ... <<END>> blocks, "
        "matching the repository's existing test framework and conventions."
    )
    user_msg = "\n".join(parts)

    try:
        text = call_llm(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=max_tokens,
            api_key=_common.runtime_override_key(api_key),
        )
    except Exception:  # noqa: BLE001 — optional/supplementary feature, degrade gracefully
        logger.warning("build_test_files: LLM call failed", exc_info=True)
        return None

    files = parse_files_from_output(text)
    if not files:
        logger.warning("build_test_files: no <<FILE>> blocks in output; len=%d head=%r", len(text), text[:300])
        return None
    logger.info("build_test_files: generated %d test file(s)", len(files))
    return files
