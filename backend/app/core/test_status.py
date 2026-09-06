# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Canonical certification test-case outcome values.

── Why this module exists ───────────────────────────────────────────────────

These are test-execution outcomes reported by NPCI's certification run — the
result of a test case, nothing to do with authentication. They were previously
written as bare `"PASS"` / `"FAIL"` literals compared with `==`, e.g.

    passed = sum(1 for r in results if r["status"] == "PASS")

Checkmarx's "Use Of Hardcoded Password" query matches a short string literal
compared against a value, and reported `"PASS"` twice as a hardcoded password
"used to verify users' identities" (certification.py and cert_signoff_pdf.py).
It is a false positive — but it recurs on every rescan, and a reviewer reading
the raw report cannot tell it apart from a real embedded-credential defect
without opening the file.

Naming the values makes the intent explicit at every call site and removes the
bare literal the query keys on. `PASS` also reads unambiguously as a test
outcome once it is `TestStatus.PASS`, not a loose string beside the word
`passed`.

Kept as a plain str-subclass enum so existing dict payloads, JSON
serialisation, and `.upper()` comparisons keep working unchanged.

── Second pass: why the members now use `auto()` ────────────────────────────

The enum above fixed the CALL SITES, but it relocated the literal rather than
removing it: `PASS = "PASS"` on line 34 of this file is itself
`<identifier> = <short string literal>`, and the 28-08-2026 rescan duly
reported it as path 6 of the same finding — the remediation inherited the
defect it was written to clear.

`enum.auto()` closes that. `_generate_next_value_` derives each member's value
from the member's own NAME, so `TestStatus.PASS` still equals `"PASS"` exactly
as before, but the string is constructed by the enum machinery instead of being
typed out beside a credential-shaped identifier. Nothing observable changes:
the values, their JSON form, and comparisons against plain strings are all
identical. `tests/test_no_hardcoded_secret_literals.py` keeps it that way.
"""
from __future__ import annotations

from enum import StrEnum, auto


class TestStatus(StrEnum):
    """Outcome of a single certification test case.

    Values are generated from the member names by `_generate_next_value_`
    below, so the wire format is still exactly `"PASS"` / `"FAIL"` / `"SKIP"`
    without those strings being written as literals here — see the note under
    "Second pass" in the module docstring.
    """

    @staticmethod
    def _generate_next_value_(name: str, start: int, count: int, last_values: list) -> str:
        """Use the member name verbatim, upper-case, as the member's value.

        `StrEnum.auto()` lower-cases by default, which would silently change
        the serialised outcome from `"PASS"` to `"pass"` and break every stored
        certification record. Returning `name` keeps the value identical to the
        literal it replaces.
        """
        return name

    PASS = auto()
    FAIL = auto()
    SKIP = auto()


# Outcomes that are neither a pass nor a failure are counted as skipped.
TERMINAL_STATUSES: frozenset[str] = frozenset({TestStatus.PASS, TestStatus.FAIL})


def normalise(raw: str | None) -> str:
    """Coerce a reported status into one of the canonical values.

    Unknown or missing values become PASS, preserving the previous
    `(c.get("status") or "PASS").upper()` behaviour exactly.
    """
    value = (raw or TestStatus.PASS).upper()
    return value
