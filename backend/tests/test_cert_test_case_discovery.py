# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Discovery of TCs from a cert_test_cases document, in both shapes the
authority emits.

The heading shape is the one that bit us live: a 24-case pack parsed to zero
cases, so the partner's readiness declaration carried an empty
`test_data_per_case` and nothing anywhere reported an error.
"""
from app.api.dashboard.certification import (
    _parse_cert_test_cases_headings,
    _parse_cert_test_cases_md,
)

# Trimmed from the real kit v1 document for change 8ebc2b48 — the shape the
# workbook renderer emits: `## <Actor> (Cn)` sheets of `### TC_n — Outcome`.
HEADING_DOC = """# Partial_AD_Compliance_Reporting_Fleet_View_Certification_Pack.xlsx

_Archetype C | 24 test cases | 3 sheets_

## Coverage audit

| API | Tag | Count |
|-----|-----|------:|
| `ReqAirworthinessCheck` | happy_path | 2 |

## Maintenance Organisation (C1)

### TC_1 — Success

**DETAILS**
```
CAAB issues ReqAirworthinessCheck to Operator during the compliance-report
guard sequence; Operator returns a SUCCESS standing.
```
**DESCRIPTION**

This case verifies the happy-path execution of the outbound airworthiness guard.

**TEST STEPS**

```
1. CAAB issues ReqAirworthinessCheck to Operator.
```

### TC_3 — Failure

**DETAILS**
```
Operator returns a FAILURE standing; RejectedException(E004) raised.
```

## Operator (C3)

### OP_2 — Failure

**DETAILS**
```
Compliance submission rejected with errorCode E008.
```
"""

TABLE_DOC = """# Cert cases

| TC ID | Scenario | Expected | Initiated By | API |
|-------|----------|----------|--------------|-----|
| TC_1 | Happy path | SUCCESS | NPCI | ReqAirworthinessCheck |
| TC_2 | Rejected   | E004    | BANK | ReqComplianceReport |
"""


def test_heading_doc_discovers_every_case():
    rows = _parse_cert_test_cases_md(HEADING_DOC)
    assert [r["tc_id"] for r in rows] == ["TC_1", "TC_3", "OP_2"]


def test_heading_doc_carries_outcome_and_details_prose():
    rows = {r["tc_id"]: r for r in _parse_cert_test_cases_md(HEADING_DOC)}
    assert rows["TC_1"]["expected"] == "Success"
    assert rows["TC_3"]["expected"] == "Failure"
    # Scenario comes from the fenced DETAILS blurb, joined onto one line.
    assert "ReqAirworthinessCheck" in rows["TC_1"]["scenario"]
    assert "guard sequence" in rows["TC_1"]["scenario"]


def test_heading_doc_does_not_guess_api_or_initiator():
    """Both are authority-owned. Guessing either is worse than leaving it
    empty: `api` drives trigger codegen, `initiated_by` decides whether the
    UI asks the operator for data at all."""
    rows = _parse_cert_test_cases_md(HEADING_DOC)
    # Assert non-empty first: without it this test loops over nothing and
    # passes on a parser that discovers no cases at all — which is the exact
    # bug it sits next to.
    assert len(rows) == 3
    for r in rows:
        assert r["api"] == ""
        assert r["initiated_by"] == ""


def test_coverage_audit_table_does_not_shadow_the_headings():
    """The document opens with a `| API | Tag | Count |` table. It has no TC-id
    column, so the table parser must yield nothing and let the heading parser
    run — otherwise the fallback never fires on a real document."""
    assert _parse_cert_test_cases_headings(HEADING_DOC)
    assert len(_parse_cert_test_cases_md(HEADING_DOC)) == 3


def test_table_shape_still_wins_when_present():
    rows = _parse_cert_test_cases_md(TABLE_DOC)
    assert [r["tc_id"] for r in rows] == ["TC_1", "TC_2"]
    assert rows[0]["api"] == "ReqAirworthinessCheck"
    assert rows[0]["initiated_by"] == "NPCI"
    assert rows[1]["initiated_by"] == "BANK"


def test_cases_carry_their_sheet_so_repeated_ids_are_distinguishable():
    """The real 24-case pack numbers TC_1..TC_8 once per actor sheet, so
    `tc_id` alone is ambiguous across sheets. Without the sheet the three
    TC_1s are indistinguishable and collapse onto one storage row."""
    rows = _parse_cert_test_cases_md(HEADING_DOC)
    by_sheet = {(r["sheet"], r["tc_id"]) for r in rows}
    assert ("Maintenance Organisation (C1)", "TC_1") in by_sheet
    assert ("Maintenance Organisation (C1)", "TC_3") in by_sheet
    assert ("Operator (C3)", "OP_2") in by_sheet
    # A case must not be attributed to the coverage-audit section above it.
    assert all(r["sheet"] != "Coverage audit" for r in rows)


def test_empty_and_caseless_documents_are_not_errors():
    assert _parse_cert_test_cases_md("") == []
    assert _parse_cert_test_cases_md("# Title\n\nJust prose, no cases.\n") == []


def test_heading_regex_ignores_non_case_h3():
    """`### Notes` is not a case id. The authority's regex requires an
    underscore-bearing identifier and an outcome word."""
    assert _parse_cert_test_cases_md("### Notes\n\ntext\n") == []
    assert _parse_cert_test_cases_md("### TC_9 — Success\n") != []
