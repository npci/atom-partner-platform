# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The sign-off PDF must carry the ACTIVE domain's identity and no other.

The PDF is the only artifact that leaves the building. Vocabulary left in a
Python docstring is an internal embarrassment; vocabulary left here is a false
statement, on a signed document, about who certified what.

HOW IT READS THE PDF. There is no PDF text-extraction library in this venv, and
adding a dependency to test one module is a poor trade. reportlab compresses
content streams by default, so `rl_config.pageCompression = 0` makes the drawn
text greppable in the raw bytes. That is a real read of the rendered output —
not an assertion about the source that produced it. The distinction matters:
code internals can measure clean while the generated document still carries the
wrong vocabulary.
"""
from __future__ import annotations

import math
import re

import pytest
from reportlab import rl_config

from app.core.domain import clear_cache
from app.services.cert_signoff_pdf import build_signoff_pdf

PACKS = "app/packs"

CASES = [
    {"test_case_id": "T1", "title": "Happy path", "initiated_by": "NPCI",
     "scenario": "success", "expected_code": "000", "actual_code": "000", "status": "PASS"},
    {"test_case_id": "T2", "title": "Refusal path", "initiated_by": "BANK",
     "scenario": "failure", "expected_code": "E01", "actual_code": "E01", "status": "PASS"},
]


@pytest.fixture(autouse=True)
def _uncompressed_and_isolated(monkeypatch):
    monkeypatch.delenv("DOMAIN_PACK", raising=False)
    clear_cache()
    old = rl_config.pageCompression
    rl_config.pageCompression = 0
    yield
    rl_config.pageCompression = old
    clear_cache()


def render(**over) -> str:
    kwargs = dict(
        partner_name="Acme Org", feature="Recall window", run_id="TR-1",
        executed_at="2026-05-01T10:00:00Z", role=None, test_data={},
        cases=CASES, summary={"total": 2, "passed": 2},
    )
    kwargs.update(over)
    return build_signoff_pdf(**kwargs).decode("latin-1")


def use(monkeypatch, name):
    monkeypatch.setenv("DOMAIN_PACK", f"{PACKS}/{name}/{name}.yaml")
    clear_cache()


def accent_applied(raw: str, hexcode: str) -> bool:
    """Is this colour actually painted in the content stream?

    reportlab writes colours as RGB floats with the leading zero stripped
    (`.086275`), and its 6-decimal rounding does not match a naive round() of
    channel/255 — comparing formatted strings gives false negatives. Compare
    numerically with a tolerance.
    """
    want = tuple(int(hexcode[i:i + 2], 16) / 255 for i in (0, 2, 4))
    for m in re.findall(r"([\d.]+) ([\d.]+) ([\d.]+) rg", raw):
        if all(math.isclose(a, float(b), abs_tol=2e-6) for a, b in zip(want, m)):
            return True
    return False


# ── The acceptance test ──────────────────────────────────────────────────────

PAYMENTS_WORDS = ["NPCI", "UPI", "VPA", "IFSC", "National Payments",
                  "Payer", "Payee", "Remitter", "Beneficiary", "PSP"]


def test_library_signoff_contains_no_payments_vocabulary(monkeypatch):
    """Install for a library network, render the document, read it.

    One occurrence of any of these words means the domain seam has a hole in it.
    """
    use(monkeypatch, "nlln")
    raw = render(
        role="LENDING_LIBRARY",
        test_data={"isbn": "9780000000001", "shelf_location": "STACK-3-A"},
    )
    present = [w for w in PAYMENTS_WORDS if w in raw]
    assert not present, f"library sign-off PDF contains payments vocabulary: {present}"


def test_library_signoff_says_who_it_actually_is(monkeypatch):
    """Absence is only half of it — the right names must be there too.

    A renderer that silently dropped every name would pass the test above.
    """
    use(monkeypatch, "nlln")
    raw = render(role="LENDING_LIBRARY", test_data={"isbn": "9780000000001"})
    for expected in ["National Library Lending Council", "NLLC",
                     "Lending Library", "ISBN", "LIBRARY"]:
        assert expected in raw, f"missing {expected!r}"
    assert accent_applied(raw, "166534")


def test_generic_signoff_is_clean_too(monkeypatch):
    raw = render()  # no DOMAIN_PACK -> generic
    present = [w for w in PAYMENTS_WORDS if w in raw]
    assert not present, f"generic sign-off PDF contains payments vocabulary: {present}"
    assert accent_applied(raw, "334155")


# ── Behaviour preservation for the existing UPI deployment ───────────────────

def test_upi_pack_still_renders_the_original_identity(monkeypatch):
    """A UPI install that opts in must get the document it got before."""
    use(monkeypatch, "upi")
    raw = render(role="PAYER_PSP", test_data={"payer_vpa": "test@examplebank"})
    # "&amp;" in the pack is Paragraph markup; reportlab renders it back to a
    # literal "&" in the content stream, so assert on the rendered form.
    for expected in ["National Payments Corporation of India",
                     "NPCI Certification & Compliance Office",
                     "UPI Certification", "Payer VPA", "Payer PSP", "NPCI", "BANK"]:
        assert expected in raw, f"missing {expected!r}"
    assert accent_applied(raw, "1d4ed8")


# ── The empty-issuer path ────────────────────────────────────────────────────

def test_empty_issuer_leaves_no_dangling_prose(monkeypatch):
    """The generic pack has no issuer, so clauses naming one must be dropped.

    The failure this guards is prose that reads "...prescribed by ." or a
    footer that renders a bare "©" — grammatically broken text on a signed
    document is worse than a missing sentence.
    """
    raw = render()
    assert "prescribed by ." not in raw
    assert "On behalf of ," not in raw
    # The copyright footer is omitted entirely rather than left as "(c) ".
    assert not re.search(r"\(\s*\xa9\s*\)", raw)
    assert "We" in raw  # the "On behalf of X, we" fallback still forms a sentence


def test_place_of_issue_comes_from_the_pack(monkeypatch):
    """`Issued at:` was the literal "Mumbai" until 2026-09-07.

    Every other identity on this document had already moved to the pack, so a
    library deployment printed its own issuer and signatory above a place of
    issue belonging to somebody else's head office.
    """
    use(monkeypatch, "upi")
    assert "Mumbai" in render()

    clear_cache()
    use(monkeypatch, "nlln")
    raw = render()
    assert "Geneva" in raw and "Mumbai" not in raw


def test_unset_place_of_issue_omits_the_clause(monkeypatch):
    """Same contract as `issuer`: omit, never invent.

    A place of issue is an assertion about where a signed document was
    executed. The generic pack has no standing to make one, so the label must
    disappear rather than render as "Issued at: ·" with an empty value.
    """
    raw = render()
    assert "Date of issue" in raw
    assert "Issued at" not in raw
    assert "Mumbai" not in raw


def test_case_without_an_initiator_is_not_attributed_to_the_authority(monkeypatch):
    """A missing `initiated_by` renders "—", not the authority's label.

    The old default was the literal "NPCI", so every case with an absent field
    became a confident claim on a signed document that the authority ran it.

    Counted rather than asserted absent: `authority_short` legitimately appears
    in the attestation prose ("N test cases across AUTHORITY-initiated and
    PARTNER-initiated flows..."), so the question is whether the ROW adds an
    occurrence, not whether the token appears at all.
    """
    use(monkeypatch, "nlln")
    base = dict(test_case_id="T9", title="No initiator", scenario="success",
                status="PASS")
    without = render(cases=[base]).count("NLLC")
    attributed = render(cases=[{**base, "initiated_by": "NPCI"}]).count("NLLC")

    assert attributed == without + 1, (
        "an explicit initiator should add exactly one labelled cell"
    )


# ── Wire tokens stay pinned, only their labels move ──────────────────────────

def test_initiator_column_shows_labels_not_wire_tokens(monkeypatch):
    """`initiated_by` is NPCI/BANK on the wire and must stay so, but the
    printed column is the pack's short label."""
    use(monkeypatch, "nlln")
    raw = render()
    assert "NLLC" in raw and "LIBRARY" in raw
    assert "NPCI" not in raw and "BANK" not in raw


def test_unknown_initiator_token_is_passed_through_not_relabelled(monkeypatch):
    """An unexpected token must appear as itself.

    Mapping it to one of the two known labels would put a confident, wrong
    attribution on the document.

    The token here is deliberately short. The column is 12mm, and reportlab
    word-wraps rather than truncates, so a longer token renders as stacked
    fragments ("REGULATOR" -> "REG"/"ULAT"/"OR") and would make this assertion
    fail for a reason that has nothing to do with the mapping. Pack-supplied
    labels are length-checked at load; a token arriving over the wire is not,
    so this documents the real behaviour rather than pretending it wraps well.
    """
    use(monkeypatch, "nlln")
    raw = render(cases=[{"test_case_id": "T9", "title": "Odd",
                         "initiated_by": "OMBUD", "status": "PASS"}])
    assert "OMBUD" in raw


# ── Identifier rows follow the pack, not a hardcoded payments list ───────────

def test_metadata_rows_use_pack_field_labels(monkeypatch):
    use(monkeypatch, "nlln")
    raw = render(role="BORROWING_LIBRARY",
                 test_data={"member_handle": "r.iyer@anna-library"})
    assert "Member handle" in raw


def test_unknown_identifier_is_humanised_not_dropped(monkeypatch):
    """A key the pack does not describe must still reach the document.

    Silently dropping a collected identifier from a signed record is worse
    than showing it under an imperfect label.
    """
    use(monkeypatch, "nlln")
    raw = render(test_data={"custody_reference": "CR-77"})
    assert "Custody reference" in raw
    assert "CR-77" in raw


def test_empty_identifier_values_are_skipped(monkeypatch):
    use(monkeypatch, "nlln")
    raw = render(test_data={"isbn": "", "shelf_location": None, "org_id": "anna-library"})
    assert "anna-library" in raw
    assert "Shelf location" not in raw


def test_pack_width_guard_matches_what_the_pdf_actually_renders(monkeypatch):
    """The guard in pack.py hardcodes the column width. This proves it is right.

    `_INITIATOR_COL_PT` and the `colWidths` entry in cert_signoff_pdf.py are two
    copies of one number in two modules. If someone re-balances the table, the
    guard silently starts accepting labels that wrap — and the only symptom is
    fragmented text on a signed PDF. So: take the longest label the guard
    ACCEPTS, render it, and assert reportlab drew it as one intact string.
    """
    from app.core.domain.pack import _check_fits_initiator_column, PackError as PE

    # Longest all-caps label the guard admits, found by widening until it trips.
    longest, w = "", ""
    for n in range(1, 20):
        candidate = "W" * n
        try:
            _check_fits_initiator_column(candidate, "partner_short", "test")
            longest = candidate
        except PE:
            break
    assert longest, "guard rejects even a single character — it is miscalibrated"

    use(monkeypatch, "nlln")
    raw = render(cases=[{"test_case_id": "T1", "title": "x",
                         "initiated_by": longest, "status": "PASS"}])
    drawn = re.findall(r"\((.*?)\)\s*Tj", raw)
    assert longest in drawn, (
        f"pack.py accepts {longest!r} but the PDF wrapped it — "
        f"_INITIATOR_COL_PT no longer matches the table's colWidths"
    )


def test_role_label_falls_back_to_the_raw_key(monkeypatch):
    """A run recorded under a role this pack no longer defines must still
    render — an empty cell on a signed document is the worst outcome."""
    use(monkeypatch, "nlln")
    raw = render(role="RETIRED_ROLE")
    assert "RETIRED_ROLE" in raw
