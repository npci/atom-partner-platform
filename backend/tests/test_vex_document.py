# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The VEX document must stay accurate, resolvable and schema-valid.

WHY THIS FILE EXISTS
====================

The other guards in this suite protect the CODE the VEX annotations describe.
Nothing protected the ANNOTATIONS themselves, and they have their own failure
modes -- ones that are silent, which makes them the dangerous kind.

An audit record that says ``not_affected`` while attaching to no component is
worse than an open finding: the finding still reports, but everybody believes it
was handled. Three concrete defects of exactly that shape were present in an
earlier revision of ``security/vex/partner-platform.vex.json``:

1. ``pkg:pypi/PyJWT@2.13.0`` -- the PyPI purl type requires a lowercase,
   PEP 503-normalised name, so this coordinate does not match the
   ``pkg:pypi/pyjwt`` that a generator emits. The pyjwt annotation would have
   attached to nothing.
2. ``analysis_reference`` -- a key that does not exist in CycloneDX 1.6. A
   strict consumer rejects the document; a lenient one drops the field. Either
   way the information is not where a reader would look for it.
3. ``justification: code_not_reachable`` on the SQLAlchemy entry, when
   ``text()`` is called on every retrieval path. The reachable thing is the
   API; the absent thing is dynamic string assembly. That is
   ``protected_by_mitigating_control``.

These tests encode the rules that would have caught all three. They deliberately
do NOT try to re-validate the prose -- that is what the code guards are for --
but they do assert that every enforcement artifact the prose cites actually
exists, because a citation to a deleted test is the same false-assurance failure
in a different costume.

This file is pure standard library and reads only JSON and the filesystem, so it
runs anywhere pytest does, with no application import and no database.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# backend/tests/ -> backend/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]
_VEX_PATH = _REPO_ROOT / "security" / "vex" / "partner-platform.vex.json"

# ── CycloneDX 1.6 enumerations ───────────────────────────────────────────────
# Transcribed from the 1.6 schema. Kept as literals rather than fetched, so the
# check works offline and cannot be softened by a network failure.

_ANALYSIS_STATES = {
    "resolved",
    "resolved_with_pedigree",
    "exploitable",
    "in_triage",
    "false_positive",
    "not_affected",
}

_JUSTIFICATIONS = {
    "code_not_present",
    "code_not_reachable",
    "requires_configuration",
    "requires_dependency",
    "requires_environment",
    "protected_by_compiler",
    "protected_at_runtime",
    "protected_at_perimeter",
    "protected_by_mitigating_control",
}

_RESPONSES = {
    "can_not_fix",
    "will_not_fix",
    "update",
    "rollback",
    "workaround_available",
}

_SEVERITIES = {
    "critical", "high", "medium", "low", "info", "none", "unknown",
}

# Keys CycloneDX 1.6 defines on a `vulnerability` object and on its `analysis`
# sub-object. Anything else is a hand-invented field -- the `analysis_reference`
# failure mode.
_VULN_KEYS = {
    "bom-ref", "id", "source", "references", "ratings", "cwes", "description",
    "detail", "recommendation", "workaround", "proofOfConcept", "advisories",
    "created", "published", "updated", "rejected", "credits", "tools",
    "analysis", "affects", "properties",
}

_ANALYSIS_KEYS = {"state", "justification", "response", "detail", "firstIssued", "lastUpdated"}


@pytest.fixture(scope="module")
def vex() -> dict:
    assert _VEX_PATH.is_file(), f"VEX document missing at {_VEX_PATH}"
    return json.loads(_VEX_PATH.read_text(encoding="utf-8"))


# ── document shape ───────────────────────────────────────────────────────────


def test_document_is_cyclonedx_1_6(vex):
    assert vex.get("bomFormat") == "CycloneDX"
    assert vex.get("specVersion") == "1.6"
    assert isinstance(vex.get("version"), int)


def test_the_three_reported_findings_are_all_annotated(vex):
    """Exactly the three Security policy violations from the 2026-08-27 report.

    If a fourth finding appears in a later scan, adding it here is the
    deliberate act of triaging it. Silence is not triage.
    """
    ids = {v["id"] for v in vex["vulnerabilities"]}
    assert ids == {"sonatype-2021-0025", "CVE-2025-45768", "sonatype-2017-0717"}


def test_no_hand_invented_fields(vex):
    """Every key must be one CycloneDX 1.6 actually defines.

    This is the check that catches `analysis_reference`. Supplementary
    information belongs in `properties[]`, which is the schema's own extension
    point, not in an invented sibling key.
    """
    for vuln in vex["vulnerabilities"]:
        unknown = set(vuln) - _VULN_KEYS
        assert not unknown, (
            f"{vuln['id']}: {sorted(unknown)} are not CycloneDX 1.6 vulnerability "
            f"fields. Use properties[] for supplementary data."
        )
        unknown_analysis = set(vuln.get("analysis", {})) - _ANALYSIS_KEYS
        assert not unknown_analysis, (
            f"{vuln['id']}: {sorted(unknown_analysis)} are not CycloneDX 1.6 "
            f"analysis fields."
        )


def test_enumerated_values_are_valid(vex):
    for vuln in vex["vulnerabilities"]:
        analysis = vuln["analysis"]
        assert analysis["state"] in _ANALYSIS_STATES, vuln["id"]
        assert analysis["justification"] in _JUSTIFICATIONS, vuln["id"]
        for response in analysis.get("response", []):
            assert response in _RESPONSES, (vuln["id"], response)
        for rating in vuln.get("ratings", []):
            assert rating.get("severity") in _SEVERITIES, (vuln["id"], rating)


# ── purl correctness: the failure that annotates nothing ─────────────────────


def _purls(vex: dict) -> list[str]:
    out = [a["ref"] for v in vex["vulnerabilities"] for a in v.get("affects", [])]
    out += [c["purl"] for c in vex.get("components", []) if "purl" in c]
    return out


def test_pypi_purls_are_pep503_normalised(vex):
    """`pkg:pypi/PyJWT` is not the same string as `pkg:pypi/pyjwt`.

    The purl spec requires the PyPI name to be lowercased and PEP 503
    normalised. A scanner matching on the coordinate finds no component for the
    non-normalised form, so the annotation attaches to nothing and the finding
    stays Unannotated -- silently.
    """
    for purl in _purls(vex):
        if not purl.startswith("pkg:pypi/"):
            continue
        name = purl[len("pkg:pypi/"):].split("@")[0]
        expected = re.sub(r"[-_.]+", "-", name).lower()
        assert name == expected, (
            f"{purl} is not PEP 503 normalised; a scanner will not match it. "
            f"Use pkg:pypi/{expected}@..."
        )


def test_npm_purls_are_lowercase(vex):
    for purl in _purls(vex):
        if not purl.startswith("pkg:npm/"):
            continue
        name = purl[len("pkg:npm/"):].split("@")[0] if not purl.startswith("pkg:npm/@") else purl.split("@")[1]
        assert name == name.lower(), f"{purl} has a non-lowercase npm name"


def test_every_purl_carries_a_version(vex):
    """An unversioned coordinate annotates every version of the package.

    That is a far broader statement than the analysis supports: the code was
    reviewed against one specific version, and a future upgrade must re-open
    the question rather than inherit the old verdict.
    """
    for purl in _purls(vex):
        coordinate = purl.split("?")[0].split("#")[0]
        assert "@" in coordinate.replace("pkg:npm/@", "pkg:npm/"), (
            f"{purl} has no version; it would annotate every release"
        )


def test_affects_refs_resolve_to_declared_components(vex):
    """A dangling `affects[].ref` is an annotation pointing at nothing."""
    declared = {c["bom-ref"] for c in vex.get("components", [])}
    assert declared, "VEX declares no components, so no ref can resolve locally"
    for vuln in vex["vulnerabilities"]:
        for affect in vuln["affects"]:
            assert affect["ref"] in declared, (
                f"{vuln['id']}: affects ref {affect['ref']!r} matches no declared "
                f"component (have: {sorted(declared)})"
            )


def test_annotated_versions_match_the_scanned_report(vex):
    """Pin the exact versions the 2026-08-27 A2A Compliance Report flagged.

    If a dependency is upgraded, this test fails and the analysis must be
    re-done for the new version rather than silently carried forward. That is
    the intended friction.
    """
    expected = {
        "pkg:pypi/sqlalchemy@2.0.36",
        "pkg:pypi/pyjwt@2.13.0",
        "pkg:npm/react@19.2.5",
    }
    actual = {a["ref"] for v in vex["vulnerabilities"] for a in v["affects"]}
    assert actual == expected, (
        "The annotated coordinates no longer match the scanned report. If a "
        "dependency moved, re-run the analysis for the new version and update "
        "both this test and the VEX -- do not just edit the version string."
    )


# ── the claims must not outlive what backs them ──────────────────────────────


def test_cited_enforcement_artifacts_exist(vex):
    """Every file named in a `vex:enforced-by` property must be present.

    A VEX that cites a deleted test is the same false-assurance failure as a
    dangling purl: it reads as though something is being checked.
    """
    seen_any = False
    for vuln in vex["vulnerabilities"]:
        for prop in vuln.get("properties", []):
            if prop.get("name") != "vex:enforced-by":
                continue
            seen_any = True
            for rel in (p.strip() for p in prop["value"].split(",")):
                assert (_REPO_ROOT / rel).is_file(), (
                    f"{vuln['id']} cites {rel}, which does not exist"
                )
    assert seen_any, "no vulnerability declares how its claim is enforced"


def test_not_affected_entries_state_a_justification(vex):
    """CycloneDX makes `justification` optional. For `not_affected` it is the
    entire substance of the claim, so it is mandatory here."""
    for vuln in vex["vulnerabilities"]:
        analysis = vuln["analysis"]
        if analysis["state"] == "not_affected":
            assert analysis.get("justification"), vuln["id"]
            assert len(analysis.get("detail", "")) > 200, (
                f"{vuln['id']}: a not_affected claim needs evidence an auditor "
                f"can check, not a sentence"
            )


def test_sqlalchemy_claim_is_not_code_not_reachable(vex):
    """Regression test for a specific overclaim that was corrected.

    `text()` IS reached, on every retrieval and ingest path. The vulnerable
    capability is present and used; what is absent is dynamic assembly of the
    string passed to it. Claiming `code_not_reachable` misdescribes the control
    and is the kind of thing that collapses under review.
    """
    entry = next(v for v in vex["vulnerabilities"] if v["id"] == "sonatype-2021-0025")
    assert entry["analysis"]["justification"] == "protected_by_mitigating_control", (
        "SQLAlchemy's text() is reachable from live code paths; the honest "
        "justification is protected_by_mitigating_control."
    )


def test_pyjwt_narrative_does_not_repeat_the_disproven_claim(vex):
    """Regression test for a factually false statement.

    An earlier revision asserted PyJWT 'deliberately refuses' key-strength
    checks and that no fix 'exists or is planned'. Both are false: 2.13.0 ships
    `jwt.warnings.InsecureKeyLengthWarning`, raised on encode and decode for
    HMAC keys under 32 bytes, citing RFC 7518 Section 3.2 -- and
    `HMACAlgorithm.prepare_key` rejects empty, PEM/SSH and JWK-shaped keys.

    The annotation must rest on the genuine residual gap (a warning does not
    halt a process, fires only at signing time, and measures length rather than
    guessability), not on a claim that the library does nothing.
    """
    entry = next(v for v in vex["vulnerabilities"] if v["id"] == "CVE-2025-45768")
    detail = entry["analysis"]["detail"]
    lowered = detail.lower()

    for phrase in ("deliberately refuses", "deliberately declines"):
        assert phrase not in lowered, (
            f"the disproven claim {phrase!r} has returned to the pyjwt analysis"
        )
    assert "no fixed version exists or is planned" not in lowered

    # And it must positively acknowledge what the library really does.
    assert "insecurekeylengthwarning" in lowered, (
        "the pyjwt analysis must acknowledge that PyJWT 2.13.0 already warns on "
        "short HMAC keys, and explain what the warning cannot do"
    )


def test_no_unsubstantiated_scale_claims(vex):
    """Numbers in an audit record must be the ones the tests actually produce.

    An earlier revision claimed 'zero false rejections' across 800,000 keys and
    equivalence 'verified exhaustively across all 32 scope combinations'.
    Neither figure was reproducible from the suite. Withdrawn and replaced with
    the measured ones; pinned here so they cannot drift back.
    """
    blob = json.dumps(vex).lower()
    assert "800,000" not in blob and "800000" not in blob
    assert "32 scope combinations" not in blob
