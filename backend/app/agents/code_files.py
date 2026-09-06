# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Code RAG Phase 3.4 — whole-file generation.

Turns an implementation plan (from `agents/code.py`) into COMPLETE file contents
that `services/git_integrator.py` commits to a merge request. A second LLM pass:
the plan says *what* changes; this pass writes the actual files, grounded in the
retrieved repo excerpts + the current contents of each file being modified.

Mirrors the NPCI `code_change.py` `<<FILE>>` whole-file convention (see
backend/app/agents/code_change.py) but standalone, matching the partner's simpler
agent shape. Pure — no DB / no GitLab here; the caller fetches current files and
pushes the result."""
from __future__ import annotations

import logging
import re

from app.agents import _common
from app.agents.prompts import load_prompt
from app.core.llm import call_llm

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = load_prompt("code_files.md")

# <<FILE: path>>\n<body>\n<<END>>  — non-greedy body, DOTALL so it spans lines.
_FILE_RE = re.compile(r"<<FILE:\s*(?P<path>.+?)\s*>>\n(?P<body>.*?)\n?<<END>>", re.DOTALL)

# A path the model must never write to (defence-in-depth against an injected
# instruction steering the commit outside the repo or into VCS internals).
_BAD_PATH = re.compile(r"(^/)|(^[A-Za-z]:)|(\.\.)|(^\.git/)|(/\.git/)")

# Whole files per LLM call. A live 21-file plan overflowed a 32k budget in one
# call, and a 6-file batch of LARGE files truncated too — output is size-bound,
# not count-bound, so batches start at 4 and `_generate_with_split` bisects any
# batch that still truncates.
_BATCH_SIZE = 4


def parse_files_from_output(text: str) -> list[dict]:
    """Extract [{path, content}] from the model's `<<FILE>>` blocks. Drops blocks
    with an empty or unsafe path. Tolerates leading prose by scanning."""
    out: list[dict] = []
    for m in _FILE_RE.finditer(text or ""):
        path = (m.group("path") or "").strip()
        # Check the raw path BEFORE normalising — stripping a leading "./" must not
        # mask a "../" or "/abs" or ".git/" that the safety check needs to see.
        if not path or _BAD_PATH.search(path):
            logger.warning("code_files: dropping block with unsafe/empty path: %r", path)
            continue
        if path.startswith("./"):
            path = path[2:]
        body = m.group("body")
        if not body.endswith("\n"):
            body += "\n"
        out.append({"path": path, "content": body})
    return out


def build_code_files(
    *,
    plan: dict,
    change_title: str,
    documents: list[dict],
    design_summary: str | None = None,
    code_context: str | None = None,
    current_files: dict[str, str] | None = None,
    api_key: str | None = None,
    on_progress=None,
) -> list[dict] | None:
    """Generate complete file contents for the plan's file changes. Returns
    [{path, content}] or None on failure. Pure (no DB / GitLab side effects);
    `on_progress(msg)` (optional) is called per batch for job-status display.

    Generation is BATCHED: whole files for a 20+-file plan don't fit one output
    budget (a real 21-file plan blew the 32k cap), so file_changes are split
    into groups of `_BATCH_SIZE` and the LLM is called once per group, each call
    seeing the full plan for context but emitting only its group's files.
    A batch that TRUNCATES (output is size-bound, not count-bound — a live
    batch of large files truncated even at 32k) bisects and retries each half,
    down to a single file which escalates once to the model's 64k ceiling.
    Strict: if any batch ultimately fails, the whole run returns None — a
    silently incomplete MR is worse than no MR."""
    profile_raw, _frontmatter = _common.read_partner_profile()
    if not profile_raw:
        logger.warning("build_code_files: partner profile missing")
        return None

    file_changes = [fc for fc in (plan.get("file_changes") or []) if isinstance(fc, dict)]
    plan_md = (plan.get("plan_markdown") or "").strip()
    if not plan_md and not file_changes:
        logger.warning("build_code_files: plan has neither plan_markdown nor file_changes")
        return None

    current_files = current_files or {}
    batches = (
        [file_changes[i:i + _BATCH_SIZE] for i in range(0, len(file_changes), _BATCH_SIZE)]
        if file_changes else [[]]
    )

    common = dict(
        profile_raw=profile_raw,
        change_title=change_title,
        design_summary=design_summary,
        plan_md=plan_md,
        code_context=code_context,
        current_files=current_files,
        api_key=api_key,
    )

    out: list[dict] = []
    seen_paths: set[str] = set()
    for bi, batch in enumerate(batches):
        if on_progress:
            on_progress(f"generating files — batch {bi + 1}/{len(batches)} ({len(batch)} files)")
        files = _generate_with_split(batch=batch, label=f"{bi + 1}/{len(batches)}", **common)
        if files is None:
            logger.error("build_code_files: batch %d/%d failed — aborting (no partial MR)",
                         bi + 1, len(batches))
            return None
        for f in files:
            if f["path"] in seen_paths:
                continue
            seen_paths.add(f["path"])
            out.append(f)

    if not out:
        logger.error("build_code_files: all batches parsed to zero files")
        return None
    logger.info("build_code_files: generated %d file(s) across %d batch(es)",
                len(out), len(batches))
    return out


def _generate_with_split(*, batch: list[dict], label: str, **common) -> list[dict] | None:
    """Generate one batch, bisecting on output truncation.

    Truncation means the batch's WHOLE-FILE output exceeded the budget — a
    same-size retry would predictably truncate again, so split instead. A
    single file that still truncates at 32k gets one escalation to the model's
    64k ceiling; if even that truncates, fail with the path named."""
    files, truncated = _generate_batch(batch=batch, label=label, max_tokens=32000, **common)
    if files is not None:
        return files
    if not truncated:
        return None  # transient failure already retried once inside _generate_batch
    if len(batch) > 1:
        mid = (len(batch) + 1) // 2
        logger.warning(
            "build_code_files: batch %s (%d files) truncated — bisecting",
            label, len(batch),
        )
        left = _generate_with_split(batch=batch[:mid], label=f"{label}a", **common)
        if left is None:
            return None
        right = _generate_with_split(batch=batch[mid:], label=f"{label}b", **common)
        if right is None:
            return None
        return left + right
    path = batch[0].get("path", "?")
    logger.warning(
        "build_code_files: single file %s truncates at 32k — escalating to 64k", path,
    )
    files, truncated = _generate_batch(batch=batch, label=f"{label}@64k", max_tokens=64000, **common)
    if files is None and truncated:
        logger.error(
            "build_code_files: %s exceeds even the 64k output ceiling — the plan "
            "likely asks for a full rewrite of a very large file", path,
        )
    return files


def _generate_batch(
    *,
    label: str,
    max_tokens: int,
    batch: list[dict],
    profile_raw: str,
    change_title: str,
    design_summary: str | None,
    plan_md: str,
    code_context: str | None,
    current_files: dict[str, str],
    api_key: str | None,
) -> tuple[list[dict] | None, bool]:
    """One LLM call emitting whole files for `batch`'s file changes only.
    Returns (files, truncated): files is None on failure; truncated=True means
    the output hit `max_tokens` (the caller bisects — a same-size retry would
    predictably truncate again). Transient errors get one same-size retry."""
    fc_lines = [
        f"- {fc.get('action', 'modify')} `{fc.get('path', '?')}` "
        f"(confidence {fc.get('confidence', '?')}): {fc.get('description', '')}"
        for fc in batch
    ]
    batch_paths = {fc.get("path") for fc in batch}

    parts: list[str] = [
        "PARTNER.md", "", profile_raw, "", "---", "",
        f"NPCI change: {change_title}",
    ]
    if design_summary:
        parts += ["", f"Design posture: {design_summary}"]
    parts += ["", "---", "", "Implementation plan (plan_markdown):", "", plan_md or "(none)"]
    if fc_lines:
        scope = f"(part {label} — other parts cover the rest of the plan)"
        parts += [
            "", "---", "",
            f"File changes to emit IN THIS RESPONSE {scope}".rstrip() + ":",
            "", *fc_lines,
            "",
            "Emit ONLY these files. Other files in the plan are generated separately "
            "— do not emit them here.",
        ]
    if code_context and code_context.strip():
        parts += [
            "", "---", "",
            "Excerpts from the partner's own repository (real paths + code). Match "
            "their conventions and reference real identifiers:",
            "", code_context,
        ]
    # Only this batch's MODIFY targets — keeps the input focused and bounded.
    batch_current = {p: c for p, c in current_files.items() if p in batch_paths}
    if batch_current:
        parts += ["", "---", "", "Current contents of files being MODIFIED "
                  "(return the FULL file with your changes applied):"]
        for path, content in batch_current.items():
            parts += ["", f"<<CURRENT: {path}>>", content, "<<ENDCURRENT>>"]
    parts += [
        "", "---", "",
        "Emit the complete file contents now, ONE `<<FILE: path>>` … `<<END>>` "
        "block per file, and nothing else.",
    ]
    user_msg = "\n".join(parts)

    # Transient drops (observed live: "peer closed connection ... incomplete
    # chunked read") get ONE same-size retry. Truncation does NOT — the output
    # is deterministically too big at this size; return truncated=True so the
    # caller bisects instead of burning another full-size call.
    text = None
    for attempt in (1, 2):
        try:
            text = call_llm(
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
                # call_llm raises on truncation rather than returning half a file.
                max_tokens=max_tokens,
                api_key=_common.runtime_override_key(api_key),
            )
            break
        except Exception as e:
            if "truncated at max_tokens" in str(e):
                logger.warning(
                    "build_code_files: batch %s truncated at %d tokens", label, max_tokens,
                )
                return None, True
            logger.warning("build_code_files: batch %s LLM call failed (attempt %d/2)", label, attempt, exc_info=True)
            if attempt == 2:
                return None, False

    files = parse_files_from_output(text)
    if not files:
        logger.error(
            "build_code_files: batch %s had no <<FILE>> blocks; len=%d head=%r",
            label, len(text), text[:300],
        )
        return None, False
    return files, False


# ── Fix pass (review-loop "Apply Fixes") ────────────────────────────────────
# Re-generate the files that have review findings, applying the fixes; carry the
# rest forward unchanged. This is the "send back to code-gen" step of the
# code-review / security-review loop — input is the prior iteration's files plus
# the merged findings, output is the corrected file set for the next iteration.

_FIX_MAX_FILE_CHARS = 24000


def _norm_path(p: str | None) -> str:
    return (p or "").strip().lstrip("./").strip()


def _findings_summary(findings: list[dict]) -> str:
    lines = []
    for x in findings or []:
        loc = x.get("file") or "?"
        if x.get("line"):
            loc += f":{x['line']}"
        lines.append(f"- [{x.get('severity')}] ({loc}) {x.get('title')}: {x.get('detail')}")
    return "\n".join(lines) or "(none)"


def _fix_one_file(
    path: str, current: str, file_findings: list[dict], all_summary: str,
    raw_profile: str, change_title: str, design_summary: str | None,
    code_context: str | None, api_key: str | None,
) -> str | None:
    """Regenerate one file with its findings applied. Returns corrected content or None."""
    fl = "\n".join(
        f"- [{x.get('severity')}] {x.get('title')}: {x.get('detail')} "
        f"(suggested fix: {x.get('suggested_fix') or 'n/a'})"
        for x in file_findings
    )
    user_msg = "\n\n".join(s for s in [
        f"# Partner profile\n{raw_profile}" if raw_profile else "",
        f"# Change\n{change_title}" if change_title else "",
        f"# Design posture\n{design_summary}" if design_summary else "",
        f"# Repository excerpts (grounding)\n{code_context}" if code_context else "",
        f"# All review findings across the change (for context)\n{all_summary}",
        f"# Findings to FIX in `{path}`\n{fl}",
        (f"# Current content of `{path}` — apply EXACTLY these fixes and nothing "
         f"else, then return the COMPLETE corrected file:\n```\n{current[:_FIX_MAX_FILE_CHARS]}\n```"),
        f"Output ONLY the corrected file as <<FILE: {path}>> ... <<END>>.",
    ] if s)

    try:
        # call_llm raises on truncation rather than returning half a file.
        text = call_llm(
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=32000,
            api_key=_common.runtime_override_key(api_key),
        )
    except Exception:
        logger.exception("fix_code_files: LLM call failed for %s", path)
        return None

    parsed = parse_files_from_output(text)
    for p in parsed:
        if _norm_path(p["path"]) == _norm_path(path):
            return p["content"]
    if len(parsed) == 1:  # model returned the file under a slightly different path
        return parsed[0]["content"]
    logger.error("fix_code_files: no usable <<FILE>> block for %s (got %d)", path, len(parsed))
    return None


def fix_code_files(
    *,
    files: list[dict],
    findings: list[dict],
    change_title: str = "",
    design_summary: str | None = None,
    code_context: str | None = None,
    api_key: str | None = None,
    on_progress=None,
) -> list[dict] | None:
    """Apply review findings to the generated files. Each file targeted by a
    finding is regenerated (corrected full content); files with no findings are
    carried forward unchanged. Returns the full new file set [{path, content}],
    or None on failure. Strict: if any targeted file fails to regenerate, the
    whole fix fails (a partially-fixed set is worse than a clear retry). Pure —
    no DB / GitLab. The caller persists the result as a new iteration."""
    if not files:
        return None
    progress = on_progress or (lambda *_: None)

    by_file: dict[str, list] = {}
    for f in findings or []:
        fp = _norm_path(f.get("file"))
        if fp:
            by_file.setdefault(fp, []).append(f)

    affected = [f for f in files if _norm_path(f["path"]) in by_file]
    if not affected:
        logger.error("fix_code_files: no finding maps to a generated file")
        return None

    raw_profile, _ = _common.read_partner_profile()
    all_summary = _findings_summary(findings)
    progress(f"applying fixes to {len(affected)} of {len(files)} files")

    out: list[dict] = []
    for f in files:
        np = _norm_path(f["path"])
        file_findings = by_file.get(np)
        if not file_findings:
            out.append({"path": f["path"], "content": f["content"]})
            continue
        progress(f"fixing {f['path']} ({len(file_findings)} finding(s))")
        corrected = _fix_one_file(
            f["path"], f["content"], file_findings, all_summary,
            raw_profile, change_title, design_summary, code_context, api_key,
        )
        if corrected is None:
            return None
        out.append({"path": f["path"], "content": corrected})
    return out
