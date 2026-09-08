# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Agent prompts must speak the ACTIVE domain's language.

Complements `test_prompt_snapshot.py`, which guards against unintended CHANGE.
This one guards against the prompts being wrong for the configured domain —
a distinction that matters, because a prompt asserting "This is UPI payments
code" to a library network is stable, snapshot-green, and useless.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from app.agents import prompts as prompts_mod
from app.core.domain import clear_cache

PROMPTS_DIR = pathlib.Path(prompts_mod.__file__).resolve().parent / "prompts"
PACKS = pathlib.Path(__file__).resolve().parents[1] / "app" / "packs"

LOADABLE = sorted(f.name for f in PROMPTS_DIR.glob("*.md") if not f.name.startswith("_"))

PAYMENTS_WORDS = ["NPCI", "UPI", "VPA", "IFSC", "TPAP", "PSP",
                  "payments", "remitter", "beneficiary"]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("DOMAIN_PACK", raising=False)
    clear_cache()
    prompts_mod.clear_cache()
    yield
    clear_cache()
    prompts_mod.clear_cache()


def use(monkeypatch, name):
    monkeypatch.setenv("DOMAIN_PACK", str(PACKS / name / f"{name}.yaml"))
    clear_cache()
    prompts_mod.clear_cache()


@pytest.mark.parametrize("name", LOADABLE)
@pytest.mark.parametrize("pack", ["generic", "upi", "nlln"])
def test_no_unresolved_placeholders(monkeypatch, pack, name):
    """Every `$var` must resolve under every shipped pack.

    `safe_substitute` deliberately leaves an unknown placeholder as literal
    `$name` rather than raising — good for resilience at runtime, but it means
    a typo'd or missing block ships a prompt containing "$authority_cap" and
    nothing fails. This is the test that turns that silence into a failure.
    """
    use(monkeypatch, pack)
    text = prompts_mod.load_prompt(name)
    leftover = sorted(set(re.findall(r"\$\{?([a-zA-Z_][a-zA-Z0-9_]*)", text)))
    assert not leftover, f"{pack}/{name}: unresolved {leftover}"


@pytest.mark.parametrize("name", LOADABLE)
def test_library_prompts_have_no_payments_vocabulary(monkeypatch, name):
    """The acceptance test for the prompt sweep.

    A library deployment's agents must not be told they work in payments.
    security_reviewer.md is the one that matters most: telling a reviewer
    "This is UPI payments code" materially steers the threat model toward
    funds movement and away from the member-privacy risks that actually apply.
    """
    use(monkeypatch, "nlln")
    text = prompts_mod.load_prompt(name)
    present = [w for w in PAYMENTS_WORDS if w.lower() in text.lower()]
    assert not present, f"{name} under the library pack still says: {present}"


@pytest.mark.parametrize("name", LOADABLE)
def test_prompts_actually_differ_between_domains(monkeypatch, name):
    """Proof the templating is live, not that the vocabulary was deleted.

    The obvious assertion — "the UPI render still contains 'UPI'" — is wrong
    for prompts that never named the domain in the first place.
    `code_reviewer.md` only ever said "at a bank", so under the UPI pack it
    renders "at a bank" with no literal "UPI" anywhere, and a word check marks
    a correctly-templated prompt as broken.

    Comparing the two renders is the assertion that actually holds: if the
    sweep had DELETED vocabulary instead of parameterising it, both packs would
    produce identical text and this fails.
    """
    use(monkeypatch, "upi")
    upi_text = prompts_mod.load_prompt(name)
    use(monkeypatch, "nlln")
    nlln_text = prompts_mod.load_prompt(name)
    assert upi_text != nlln_text, (
        f"{name} renders identically under both packs — its domain wording was "
        f"deleted rather than moved into the pack"
    )


def test_security_reviewer_threat_model_follows_the_domain(monkeypatch):
    """The single most consequential prompt line in this sweep."""
    use(monkeypatch, "upi")
    assert "UPI payments code" in prompts_mod.load_prompt("security_reviewer.md")

    use(monkeypatch, "nlln")
    nlln = prompts_mod.load_prompt("security_reviewer.md")
    assert "member privacy" in nlln and "loan accounting" in nlln
    assert "payments" not in nlln.lower()


def test_prompt_files_carry_no_hardcoded_authority_name():
    """Source-level check, independent of any pack.

    The rendered-output tests above would still pass if one prompt hardcoded
    "NPCI" and the active pack happened to be UPI. This asserts the templates
    themselves are clean, which is what makes the pack the single source.
    """
    offenders = {}
    for f in sorted(PROMPTS_DIR.glob("*.md")):
        hits = [w for w in ("NPCI", "UPI", "VPA", "IFSC", "TPAP")
                if w in f.read_text(encoding="utf-8")]
        if hits:
            offenders[f.name] = hits
    assert not offenders, f"prompt templates still hardcode domain terms: {offenders}"
