# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Byte-identity guard for prompt text.

WHAT THIS IS FOR. Agent prompts must take their domain vocabulary from the
active pack rather than asserting it in their opening lines. This test hashes
every prompt surface and fails if any byte moves, so an intended rewording is
distinguishable from an accident: a diff here that you cannot explain IS the
accident.

WHY BYTES AND NOT MEANING. This cannot tell you a prompt got WORSE — only that
it changed. Deliberate rewording fails this test by design; you re-bless and put
the WHY in the commit message. A snapshot diff with no explanation is
indistinguishable from a mistake.

FOUR SURFACES, because the prompt is not only the .md file:

  1. `prompts/*.md` raw bytes.
  2. The COMPOSED text `load_prompt(name)` returns. `_PREAMBLE_PROMPTS` in
     app/agents/prompts.py prefixes some prompts with `_principles_preamble.md`;
     adding or removing a name there changes the effective prompt while every
     .md file stays byte-identical. Surface 1 alone would call that green.
     `load_prompt` also injects the domain pack's vocabulary, so this surface
     covers the interpolated result, not the template.
  3. Module-level string constants >= MIN_PROMPT_CHARS in `app/agents/*.py`.
     On the authority platform the same sweep correctly templated a *system*
     prompt while the *user* prompt a few lines below kept its hardcoded
     tokens — the model was then told to emit values that failed normalisation
     and silently fell back. Those user prompts are Python constants, not .md
     files, so hashing them makes that failure mode visible instead of silent.
  4. Prompts BUILT BY A FUNCTION. Three prompts moved out of constants and into
     builders when they started reading the domain pack, because a constant
     freezes whichever pack was active at import. That refactor is invisible to
     surface 3 — it removes keys rather than changing them — so surface 4 is
     what stops a prompt becoming unguarded while every test stays green. See
     `_BUILDERS`.

RE-BLESS after an intentional change:

    UPDATE_PROMPT_SNAPSHOT=1 .venv/bin/python -m pytest tests/test_prompt_snapshot.py

then commit the regenerated prompt_snapshot.json alongside the prompt edit.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import pathlib

import pytest

AGENTS = pathlib.Path(__file__).resolve().parents[1] / "app" / "agents"
PROMPTS_DIR = AGENTS / "prompts"
SNAPSHOT = pathlib.Path(__file__).resolve().parent / "prompt_snapshot.json"

# Chars. Below this a literal is a log line, an error message or a docstring
# fragment, not a prompt. Changing this rewrites every key — treat it as frozen.
MIN_PROMPT_CHARS = 200

# (module under app.agents, zero-arg function returning a system prompt).
# Add to this list whenever a prompt moves from a constant into a builder;
# `test_guard_covers_every_prompt_file` asserts the known ones stay reachable.
_BUILDERS = [
    ("test_data_suggester", "_system_prompt"),
    ("question_suggester", "_system_prompt"),
    ("revision_context", "_summary_system"),
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _literal_size(node: ast.expr) -> int:
    """Literal chars in a string / f-string / `+`-concatenated tree (0 if not one).

    f-strings and implicit concatenation both appear in these prompts, so a
    plain `isinstance(node, ast.Constant)` check would miss most of them.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return len(node.value)
    if isinstance(node, ast.JoinedStr):
        return sum(len(p.value) for p in node.values
                   if isinstance(p, ast.Constant) and isinstance(p.value, str))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_size(node.left) + _literal_size(node.right)
    return 0


def _collect() -> dict[str, str]:
    """Hash every prompt surface. Keys are stable, sorted, and name their origin."""
    out: dict[str, str] = {}

    # Surface 1 — raw prompt files.
    for f in sorted(PROMPTS_DIR.glob("*.md")):
        out[f"file:{f.name}"] = _sha(f.read_text(encoding="utf-8"))

    # Surface 2 — composed text, preamble included. Imported lazily so a syntax
    # error in the loader surfaces as a normal test failure, not a collection
    # error that skips the whole file.
    from app.agents.prompts import clear_cache, load_prompt

    clear_cache()  # do not trust an lru_cache warmed by an earlier test
    for f in sorted(PROMPTS_DIR.glob("*.md")):
        if f.name.startswith("_"):
            continue  # a fragment, never loaded on its own
        out[f"composed:{f.name}"] = _sha(load_prompt(f.name))

    # Surface 4 — prompts BUILT BY A FUNCTION rather than stored in a constant.
    # Three of these are functions rather than constants because they read the
    # domain pack (a constant would freeze whichever pack was active at
    # import). That shape moves them out of surface 3's reach, so they need
    # covering explicitly here — otherwise an edit to a prompt silently loses
    # its guard. Losing coverage while every test stays green is exactly the
    # failure this file exists to prevent.
    #
    # Rendered under one explicit pack so the hash is deterministic: the point
    # is to catch an edit to the prompt, not to notice DOMAIN_PACK changed.
    prev = os.environ.get("DOMAIN_PACK")
    os.environ["DOMAIN_PACK"] = str(
        pathlib.Path(__file__).resolve().parents[1] / "app" / "packs" / "upi" / "upi.yaml"
    )
    try:
        from app.core.domain import clear_cache as clear_pack

        clear_pack()
        for mod_name, fn_name in _BUILDERS:
            mod = importlib.import_module(f"app.agents.{mod_name}")
            out[f"built:{mod_name}:{fn_name}"] = _sha(getattr(mod, fn_name)())
    finally:
        if prev is None:
            os.environ.pop("DOMAIN_PACK", None)
        else:
            os.environ["DOMAIN_PACK"] = prev
        from app.core.domain import clear_cache as clear_pack

        clear_pack()

    # Surface 3 — module-level string constants in agent modules.
    for py in sorted(AGENTS.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken module fails elsewhere
            continue
        rel = py.relative_to(AGENTS)
        for node in tree.body:  # module level only — not nested in a function
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            # AnnAssign without a value (`X: str`) has node.value is None.
            if node.value is None or _literal_size(node.value) < MIN_PROMPT_CHARS:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    out[f"const:{rel}:{t.id}"] = _sha(ast.unparse(node.value))
    return out


def test_prompt_text_is_unchanged():
    current = _collect()

    if os.environ.get("UPDATE_PROMPT_SNAPSHOT"):
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
        pytest.skip(f"snapshot re-blessed: {len(current)} entries written")

    assert SNAPSHOT.exists(), (
        f"{SNAPSHOT.name} is missing. Generate it with:\n"
        "  UPDATE_PROMPT_SNAPSHOT=1 .venv/bin/python -m pytest "
        "tests/test_prompt_snapshot.py"
    )
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    added = sorted(set(current) - set(expected))
    removed = sorted(set(expected) - set(current))
    changed = sorted(k for k in set(current) & set(expected)
                     if current[k] != expected[k])

    if added or removed or changed:
        lines = ["Prompt text changed. If this was deliberate, re-bless the "
                 "snapshot and say WHY in the commit message.", ""]
        for k in changed:
            lines.append(f"  CHANGED  {k}")
        for k in added:
            lines.append(f"  ADDED    {k}")
        for k in removed:
            lines.append(f"  REMOVED  {k}")
        lines += ["", "Re-bless with:",
                  "  UPDATE_PROMPT_SNAPSHOT=1 .venv/bin/python -m pytest "
                  "tests/test_prompt_snapshot.py"]
        pytest.fail("\n".join(lines))


def test_guard_covers_every_prompt_file():
    """The snapshot is worthless if it silently stops seeing prompts.

    A glob that matches nothing, a renamed prompts/ directory, or a loader
    refactor would each leave `test_prompt_text_is_unchanged` cheerfully green
    over an empty set. Assert the surfaces are actually populated.
    """
    keys = _collect()
    md = list(PROMPTS_DIR.glob("*.md"))
    assert len(md) >= 10, f"expected the 10 known agent prompts, found {len(md)}"
    assert sum(k.startswith("file:") for k in keys) == len(md)
    assert sum(k.startswith("composed:") for k in keys) >= 9  # all but _preamble
    # design_alignment._SYSTEM_PROMPT is the largest remaining inline constant;
    # if constant scraping breaks, this is the first thing to stop being seen.
    assert "const:design_alignment.py:_SYSTEM_PROMPT" in keys, (
        "module-level prompt constants are no longer being collected — "
        "surface 3 has silently stopped guarding anything"
    )
    # Every builder must render. A prompt moved out of a constant and into a
    # function is invisible to surface 3, so this is the only thing standing
    # between that refactor and an unguarded prompt.
    for mod_name, fn_name in _BUILDERS:
        assert f"built:{mod_name}:{fn_name}" in keys, (
            f"{mod_name}.{fn_name} is no longer being hashed — surface 4 has "
            f"stopped guarding a prompt that is not stored as a constant"
        )
