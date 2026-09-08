# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The seed profile template and its worked examples.

`data/partner_profile.template.md` is the file every fresh install is told to
fill in. It is bind-mounted, DB-seeded on first boot, and read as LLM context by
the feasibility analyser — so a template that presumes one industry quietly
makes that industry the only one the platform serves well.
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DATA = REPO / "data"
TEMPLATE = DATA / "partner_profile.template.md"
EXAMPLES = DATA / "examples"
FEASIBILITY = REPO / "backend" / "app" / "agents" / "prompts" / "feasibility.md"

PAYMENTS_WORDS = ["UPI", "NPCI", "VPA", "IFSC", "PSP", "TPAP", "CBS",
                  "payments", "remitter", "beneficiary"]


def test_template_is_domain_neutral():
    """The structure presumed payments: "§1 Identity and roles in UPI",
    "UPI roles played", "UPI switch", "§9 Recent UPI rollouts"."""
    text = TEMPLATE.read_text(encoding="utf-8")
    present = [w for w in PAYMENTS_WORDS if w.lower() in text.lower()]
    assert not present, f"profile template still presumes a domain: {present}"


def _cited_sections(prompt: str) -> set[str]:
    """Section numbers the feasibility prompt cites, e.g. §2 / §6."""
    return set(re.findall(r"§(\d+)", prompt))


def _template_sections(text: str) -> set[str]:
    return set(re.findall(r"^##\s+(\d+)\.", text, re.M))


def test_every_section_the_prompt_cites_exists_in_the_template():
    """The numbering is a CONTRACT, not formatting.

    feasibility.md cites §1, §2, §4, §5, §6 and §8 by number when it explains
    what to reason about, and asks the model to cite them back in its findings.
    Renumbering or dropping a section degrades those citations to references to
    nothing — silently, because the prompt still renders and the model still
    answers. This is the test that makes that a build failure.
    """
    cited = _cited_sections(FEASIBILITY.read_text(encoding="utf-8"))
    have = _template_sections(TEMPLATE.read_text(encoding="utf-8"))
    # §12 appears in the prompt's prose as an upper bound ("§1–§12") rather than
    # as a real citation; only assert on sections it actually points at.
    missing = {s for s in cited if s in {"1", "2", "4", "5", "6", "8"}} - have
    assert not missing, (
        f"feasibility.md cites §{sorted(missing)} but the template has no such "
        f"section — those citations now reference nothing"
    )


def test_template_warns_against_renumbering():
    """The contract above is only discoverable if the template says so."""
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "KEEP THE SECTION NUMBERS" in text
    assert "feasibility.md" in text


# ── Worked examples ──────────────────────────────────────────────────────────

def test_two_examples_ship_covering_different_domains():
    """One example makes the structure look industry-specific.

    A single banking dossier was the only shipped example, which made the
    §1–§11 structure read as payments scaffolding. Two examples in genuinely
    different domains demonstrate it is not.
    """
    names = {p.name for p in EXAMPLES.glob("*.md")}
    assert "example_bank_profile.md" in names
    assert "example_library_profile.md" in names


def test_library_example_is_free_of_payments_vocabulary():
    text = (EXAMPLES / "example_library_profile.md").read_text(encoding="utf-8")
    present = [w for w in PAYMENTS_WORDS if w.lower() in text.lower()]
    assert not present, f"library example contains payments vocabulary: {present}"


@pytest.mark.parametrize("name", ["example_bank_profile.md",
                                  "example_library_profile.md"])
def test_examples_follow_the_template_structure(name):
    """An example that drifts from the template teaches the wrong shape."""
    have = _template_sections((EXAMPLES / name).read_text(encoding="utf-8"))
    expected = _template_sections(TEMPLATE.read_text(encoding="utf-8"))
    assert expected <= have, f"{name} is missing sections {sorted(expected - have)}"


@pytest.mark.parametrize("name", ["example_bank_profile.md",
                                  "example_library_profile.md"])
def test_examples_declare_themselves_fictional(name):
    """TRADEMARKS.md asserts both are fictional. Keep that true.

    The previous example named a real bank and needed a nominative-use
    rationale; the disclaimer must not drift back out of sync with the files.
    """
    text = (EXAMPLES / name).read_text(encoding="utf-8").upper()
    assert "FICTIONAL" in text


def test_no_stale_reference_to_the_removed_hdfc_example():
    """Three tracked files pointed at data/examples/hdfc_profile.md, which is
    not in the repository — including TRADEMARKS.md, which described it as
    naming a real bank under nominative use."""
    offenders = []
    for p in [REPO / "ARCHITECTURE.md", REPO / "TRADEMARKS.md",
              REPO / "DEPLOYMENT_GUIDE.md", TEMPLATE]:
        if p.exists() and "hdfc_profile.md" in p.read_text(encoding="utf-8"):
            offenders.append(p.name)
    assert not offenders, f"stale reference to a non-existent example: {offenders}"
