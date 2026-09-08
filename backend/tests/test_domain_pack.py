# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the domain seam (app/core/domain).

The seam's whole job is that code stops knowing which domain it is running in.
These tests therefore care much more about the FAILURE behaviours than the
happy path: a pack that loads is obvious, a pack that silently loads the WRONG
vocabulary is the bug that ships a payments PDF to a library network.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.domain import (
    DEFAULT_PACK,
    PackError,
    active_pack_path,
    clear_cache,
    get_active_pack,
    prompt_block,
)
from app.core.domain.pack import load

PACKS = Path(__file__).resolve().parents[1] / "app" / "packs"


@pytest.fixture(autouse=True)
def _isolate_pack_selection(monkeypatch):
    """Every test starts with no DOMAIN_PACK and a cold cache.

    The registry caches on path for the life of the process, so without this a
    test that sets DOMAIN_PACK leaks its pack into every test that runs after
    it — and the leak is invisible, because the wrong pack still loads fine.
    """
    monkeypatch.delenv("DOMAIN_PACK", raising=False)
    clear_cache()
    yield
    clear_cache()


# ── Shipped packs ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["generic", "upi", "nlln"])
def test_shipped_packs_load(name):
    pack = load(PACKS / name / f"{name}.yaml")
    assert pack.name == name


def test_default_is_generic_not_upi():
    """An unconfigured deployment must NOT get payments vocabulary.

    This is the single most important assertion here. Defaulting to `upi`
    would be the convenient choice — it preserves today's behaviour for the
    existing install — and it would mean every new deployment silently starts
    out branded, which is the exact failure this work exists to remove.
    """
    assert active_pack_path() == DEFAULT_PACK
    assert get_active_pack().name == "generic"
    assert get_active_pack().roles == ()


def test_domain_pack_env_selects_by_path(monkeypatch):
    monkeypatch.setenv("DOMAIN_PACK", str(PACKS / "nlln" / "nlln.yaml"))
    clear_cache()
    assert get_active_pack().name == "nlln"
    assert prompt_block("authority") == "the NLLC"


# ── Failure behaviour ────────────────────────────────────────────────────────

def test_missing_pack_raises_rather_than_falling_back(monkeypatch, tmp_path):
    """A bad DOMAIN_PACK must be loud.

    Falling back to the default would let a deployment that meant to run one
    domain quietly generate documents in another's vocabulary — prose that
    reads plausibly and is wrong, discovered by a reader rather than by CI.
    """
    monkeypatch.setenv("DOMAIN_PACK", str(tmp_path / "nope.yaml"))
    clear_cache()
    with pytest.raises(PackError, match="no such pack file"):
        get_active_pack()


def test_malformed_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("prompt_blocks: [unclosed\n", encoding="utf-8")
    with pytest.raises(PackError, match="not valid YAML"):
        load(p)


def test_non_mapping_pack_raises(tmp_path):
    p = tmp_path / "list.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(PackError, match="must be a YAML mapping"):
        load(p)


def test_prompt_blocks_must_be_a_mapping(tmp_path):
    p = tmp_path / "b.yaml"
    p.write_text("pack: x\nprompt_blocks:\n  - one\n", encoding="utf-8")
    with pytest.raises(PackError, match="`prompt_blocks` must be a mapping"):
        load(p)


def test_duplicate_role_key_rejected(tmp_path):
    """Two roles with one key would make role() return the first and drop the
    second's identifier fields — a cert run collected against the wrong schema."""
    p = tmp_path / "dup.yaml"
    p.write_text(
        "pack: x\nroles:\n"
        "  - key: A\n    label: First\n"
        "  - key: A\n    label: Second\n",
        encoding="utf-8",
    )
    with pytest.raises(PackError, match="duplicate role key"):
        load(p)


@pytest.mark.parametrize("field", ["authority_short", "partner_short"])
def test_overlong_initiator_label_rejected(tmp_path, field):
    """These label a narrow column in the sign-off PDF.

    reportlab word-wraps rather than truncates, so an over-long value renders
    as stacked fragments on a signed document. Caught at load, where the pack
    author can still fix it, rather than by whoever opens the PDF.
    """
    p = tmp_path / "long.yaml"
    p.write_text(f"pack: x\nsignoff:\n  {field}: SUPERINTENDENT\n", encoding="utf-8")
    with pytest.raises(PackError, match="word-wraps"):
        load(p)


def test_initiator_label_is_measured_not_counted(tmp_path):
    """Two labels of identical LENGTH, only one of which fits.

    Helvetica is proportional. A character cap would either admit "OMBUDSMAN"
    (which wraps) or reject "REGISTRAR" (which does not); both are nine
    characters. This is the test that stops someone "simplifying" the width
    measurement back into a len() check.
    """
    def write(v):
        q = tmp_path / f"{v}.yaml"
        q.write_text(f"pack: x\nsignoff:\n  partner_short: {v}\n", encoding="utf-8")
        return q

    assert load(write("REGISTRAR")).signoff.partner_short == "REGISTRAR"
    with pytest.raises(PackError, match="word-wraps"):
        load(write("OMBUDSMAN"))


def test_shipped_packs_have_labels_that_fit():
    for name in ("generic", "upi", "nlln"):
        load(PACKS / name / f"{name}.yaml")  # would raise at load if not


def test_role_without_key_rejected(tmp_path):
    p = tmp_path / "nokey.yaml"
    p.write_text("pack: x\nroles:\n  - label: Nameless\n", encoding="utf-8")
    with pytest.raises(PackError, match="missing a `key`"):
        load(p)


# ── prompt_block contract ────────────────────────────────────────────────────

def test_missing_block_returns_default_and_never_raises():
    """A domain that lacks a concept supplies no block for it, and that is a
    valid pack. Callers must be able to rely on a default rather than guarding
    every lookup."""
    pack = get_active_pack()
    assert pack.prompt_block("no_such_block_anywhere", "fallback") == "fallback"
    assert pack.prompt_block("no_such_block_anywhere") == ""


def test_scalar_blocks_coerce_to_text(tmp_path):
    """A YAML scalar that parses as a number or bool must still interpolate as
    text — otherwise `version: 2.0` renders into a prompt as `2.0` via str()
    at best, and raises on concatenation at worst."""
    p = tmp_path / "s.yaml"
    p.write_text("pack: x\nprompt_blocks:\n  n: 2.0\n  b: yes\n  e:\n", encoding="utf-8")
    pack = load(p)
    assert pack.prompt_block("n") == "2.0"
    assert pack.prompt_block("b") == "True"
    assert pack.prompt_block("e") == ""


# ── Behaviour preservation for the existing UPI deployment ───────────────────

def test_upi_pack_carries_the_wording_currently_hardcoded_in_prompts():
    """The UPI pack holds the exact wording a UPI deployment renders.

    If someone edits the pack and drifts these strings, a UPI deployment starts
    rendering something other than what it is supposed to — with nothing else
    in the suite noticing, because every other test asserts the mechanism
    rather than the wording.
    """
    pack = load(PACKS / "upi" / "upi.yaml")
    assert pack.prompt_block("authority") == "NPCI"
    assert pack.prompt_block("partner_descriptor") == (
        "a UPI ecosystem partner (a bank, PSP, or TPAP)"
    )
    assert pack.prompt_block("partner_employer") == (
        "a bank / PSP that participates in the NPCI UPI ecosystem"
    )
    assert pack.prompt_block("code_context") == "This is UPI payments code — the bar is high."


def test_upi_roles_match_the_four_the_ui_offers():
    pack = load(PACKS / "upi" / "upi.yaml")
    assert [r.key for r in pack.roles] == [
        "PAYER_PSP", "PAYEE_PSP", "REMITTER_BANK", "BENEFICIARY_BANK",
    ]
    assert [r.case_prefix for r in pack.roles] == ["PR_", "PE_", "RE_", "BE_"]

    payer = pack.role("PAYER_PSP")
    assert [f.key for f in payer.fields] == ["payer_vpa", "mobile_number"]
    assert payer.fields[0].required is True
    assert payer.fields[1].required is False


def test_shipped_packs_name_no_real_institution():
    """Placeholders must not ship a third party's identifiers.

    The role placeholders previously read `test@sbi` and `SBIN0000001`, which
    name State Bank of India. Illustrating a VPA does not require a real bank.
    """
    banned = ("sbi", "hdfc", "icici", "axis", "phonepe", "paytm", "npci-uat")
    for name in ("generic", "upi", "nlln"):
        pack = load(PACKS / name / f"{name}.yaml")
        for role in pack.roles:
            for f in role.fields:
                assert not any(b in f.placeholder.lower() for b in banned), (
                    f"{name}: role {role.key} field {f.key} placeholder "
                    f"{f.placeholder!r} names a real institution"
                )


# ── The seam is domain-general, demonstrated rather than asserted ────────────

def test_two_packs_disagree_on_every_vocabulary_axis():
    """If upi and nlln returned similar wording the seam would be decorative.

    Comparing them is what shows the vocabulary is genuinely pack-supplied and
    not partly baked into a shared default.
    """
    upi = load(PACKS / "upi" / "upi.yaml")
    nlln = load(PACKS / "nlln" / "nlln.yaml")

    for block in ("authority", "partner_descriptor", "partner_employer",
                  "domain_name", "change_descriptor", "code_context"):
        assert upi.prompt_block(block) != nlln.prompt_block(block), block

    assert {r.key for r in upi.roles}.isdisjoint({r.key for r in nlln.roles})
    assert upi.signoff.issuer != nlln.signoff.issuer
    assert upi.signoff.accent != nlln.signoff.accent


def test_non_upi_packs_are_free_of_payments_vocabulary():
    """The acceptance test in miniature, at the pack level.

    A library deployment must not find payments words anywhere in the
    vocabulary it will render.
    """
    banned = ("upi", "vpa", "ifsc", "npci", "psp", "remitter", "beneficiary")
    for name in ("generic", "nlln"):
        raw = (PACKS / name / f"{name}.yaml").read_text(encoding="utf-8").lower()
        # Strip comment lines: the header comments legitimately DISCUSS the
        # payments terms in order to explain what the pack avoids, and counting
        # those would make the rule unwritable.
        body = "\n".join(ln for ln in raw.splitlines()
                         if not ln.lstrip().startswith("#"))
        for term in banned:
            assert term not in body, f"{name}.yaml body contains {term!r}"


# ── Caching ──────────────────────────────────────────────────────────────────

def test_pack_is_parsed_once_per_path(monkeypatch):
    monkeypatch.setenv("DOMAIN_PACK", str(PACKS / "upi" / "upi.yaml"))
    clear_cache()
    assert get_active_pack() is get_active_pack()


def test_clear_cache_allows_reselection(monkeypatch):
    monkeypatch.setenv("DOMAIN_PACK", str(PACKS / "upi" / "upi.yaml"))
    clear_cache()
    assert get_active_pack().name == "upi"
    monkeypatch.setenv("DOMAIN_PACK", str(PACKS / "nlln" / "nlln.yaml"))
    clear_cache()
    assert get_active_pack().name == "nlln"


# ── document_prefix ──────────────────────────────────────────────────────────

def test_document_prefix_keeps_non_latin_programme_names():
    """A configured pack must never fall through to the unconfigured default.

    The first implementation filtered on `[A-Za-z0-9]`, which deleted every
    character of a non-Latin `programme` — the result was empty and fell back to
    "Certification". So a fully configured Japanese or Greek deployment saw its
    programme on the PDF masthead and a generic name on the downloaded file:
    exactly the drift this method exists to prevent, hitting configured packs
    rather than unconfigured ones.
    """
    from app.core.domain import Signoff

    assert Signoff(programme="認証プログラム").document_prefix() == "認証プログラム"
    assert Signoff(programme="Πιστοποίηση").document_prefix() == "Πιστοποίηση"
    assert Signoff(programme="Zertifizierung für Ü").document_prefix() == "Zertifizierung_für_Ü"


def test_document_prefix_preserves_hyphens_and_neutralises_header_injection():
    """`-` is legal in a filename and carries meaning; quotes and newlines are
    the reason this method sanitises at all, since the value reaches a
    Content-Disposition header."""
    from app.core.domain import Signoff

    assert (Signoff(programme="NLLN Lending-Protocol Certification").document_prefix()
            == "NLLN_Lending-Protocol_Certification")

    for hostile in ('"; rm -rf /', "a\nb", 'x"y', "p/../q"):
        out = Signoff(programme=hostile).document_prefix()
        assert not any(c in out for c in '"\n\r/\\'), out

    assert Signoff(programme="").document_prefix() == "Certification"
    assert Signoff(programme="   ").document_prefix() == "Certification"
