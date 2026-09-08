# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""GET /api/domain — the pack vocabulary the SPA builds its role picker from.

The Declare Ready dialog used to hardcode four UPI roles and their VPA / IFSC /
MCC fields. That was not a cosmetic problem: a non-payments deployment could not
select a VALID role at all, so nothing downstream could be exercised. These
tests assert the endpoint actually varies with the pack, and that the empty case
is honest rather than silently offering nothing.
"""
from __future__ import annotations

import pathlib

import pytest

from app.core.domain import clear_cache

PACKS = pathlib.Path(__file__).resolve().parents[1] / "app" / "packs"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("DOMAIN_PACK", raising=False)
    clear_cache()
    yield
    clear_cache()


def call(monkeypatch, pack: str | None = None) -> dict:
    from app.api.dashboard.domain import get_domain

    if pack:
        monkeypatch.setenv("DOMAIN_PACK", str(PACKS / pack / f"{pack}.yaml"))
    clear_cache()
    # The endpoint reads no user state; the dependency is there so this route
    # is not the lone unauthenticated exception under /api.
    return get_domain(user=None)


def test_upi_pack_exposes_the_four_original_roles(monkeypatch):
    d = call(monkeypatch, "upi")
    assert [r["key"] for r in d["roles"]] == [
        "PAYER_PSP", "PAYEE_PSP", "REMITTER_BANK", "BENEFICIARY_BANK",
    ]
    assert [r["case_prefix"] for r in d["roles"]] == ["PR_", "PE_", "RE_", "BE_"]
    payer = d["roles"][0]
    assert [f["key"] for f in payer["fields"]] == ["payer_vpa", "mobile_number"]
    assert payer["fields"][0]["required"] is True
    assert payer["fields"][1]["required"] is False


def test_library_pack_exposes_library_roles(monkeypatch):
    d = call(monkeypatch, "nlln")
    assert [r["key"] for r in d["roles"]] == [
        "LENDING_LIBRARY", "BORROWING_LIBRARY", "MEMBER_REGISTRY",
    ]
    keys = {f["key"] for r in d["roles"] for f in r["fields"]}
    assert "isbn" in keys and "member_handle" in keys
    # The reason this endpoint exists.
    assert not (keys & {"payer_vpa", "payee_vpa", "ifsc", "merchant_category_code"})


def test_generic_pack_returns_no_roles_rather_than_inventing_any(monkeypatch):
    """An empty list is the correct answer, and the SPA renders a message for it.

    Shipping four placeholder roles would let the dialog present selections that
    mean nothing and collect identifiers nobody can supply.
    """
    d = call(monkeypatch)
    assert d["pack"] == "generic"
    assert d["roles"] == []


def test_roles_are_ordered_as_the_pack_declares_them(monkeypatch):
    """The SPA defaults to roles[0], so order is behaviour, not presentation."""
    d = call(monkeypatch, "upi")
    assert d["roles"][0]["key"] == "PAYER_PSP"


def test_field_labels_match_what_the_signoff_pdf_prints(monkeypatch):
    """One definition, two consumers — the dialog and the signed document.

    If these diverged, an operator would type a value under one label and see it
    printed under another.
    """
    from app.core.domain import get_active_pack

    monkeypatch.setenv("DOMAIN_PACK", str(PACKS / "upi" / "upi.yaml"))
    clear_cache()
    d = get_domain_via_module()
    pack = get_active_pack()
    for role in d["roles"]:
        for f in role["fields"]:
            assert pack.field_label(f["key"]) == f["label"]


def get_domain_via_module() -> dict:
    from app.api.dashboard.domain import get_domain

    return get_domain(user=None)
