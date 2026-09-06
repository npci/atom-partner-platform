# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for review_base.sanitize_findings, including the SDLC Gap 4
root_cause/principle_ref enrichment (docs/ARCHITECTURE_REVIEW_ACTIONS.md,
Tier 3)."""
from app.agents.review_base import sanitize_findings


class TestBasicSanitization:
    def test_none_input_is_invalid(self):
        assert sanitize_findings(None) is None

    def test_non_dict_input_is_invalid(self):
        assert sanitize_findings(["not", "a", "dict"]) is None

    def test_missing_findings_key_is_invalid(self):
        assert sanitize_findings({"summary": "x"}) is None

    def test_findings_not_a_list_is_invalid(self):
        assert sanitize_findings({"summary": "x", "findings": "not-a-list"}) is None

    def test_empty_findings_list_is_valid_and_clean(self):
        out = sanitize_findings({"summary": "clean", "findings": []})
        assert out is not None
        assert out["findings"] == []

    def test_drops_non_dict_findings(self):
        out = sanitize_findings({"summary": "x", "findings": ["not a dict", {"title": "real"}]})
        assert len(out["findings"]) == 1

    def test_drops_findings_with_no_title_or_detail(self):
        out = sanitize_findings({"summary": "x", "findings": [{"severity": "high"}]})
        assert out["findings"] == []

    def test_keeps_finding_with_only_detail(self):
        out = sanitize_findings({"summary": "x", "findings": [{"detail": "something wrong"}]})
        assert len(out["findings"]) == 1

    def test_normalizes_invalid_severity_to_medium(self):
        out = sanitize_findings({"summary": "x", "findings": [{"title": "t", "severity": "made_up"}]})
        assert out["findings"][0]["severity"] == "medium"

    def test_normalizes_severity_case(self):
        out = sanitize_findings({"summary": "x", "findings": [{"title": "t", "severity": "CRITICAL"}]})
        assert out["findings"][0]["severity"] == "critical"


class TestRootCauseAndPrincipleRefEnrichment:
    def test_defaults_root_cause_to_none_when_absent(self):
        out = sanitize_findings({"summary": "x", "findings": [{"title": "t"}]})
        assert out["findings"][0]["root_cause"] is None

    def test_defaults_principle_ref_to_none_when_absent(self):
        out = sanitize_findings({"summary": "x", "findings": [{"title": "t"}]})
        assert out["findings"][0]["principle_ref"] is None

    def test_preserves_model_supplied_root_cause(self):
        out = sanitize_findings({
            "summary": "x",
            "findings": [{"title": "t", "root_cause": "caller passes unvalidated input"}],
        })
        assert out["findings"][0]["root_cause"] == "caller passes unvalidated input"

    def test_preserves_model_supplied_principle_ref(self):
        out = sanitize_findings({
            "summary": "x",
            "findings": [{"title": "t", "principle_ref": "security_architecture_skills.md §9.1"}],
        })
        assert out["findings"][0]["principle_ref"] == "security_architecture_skills.md §9.1"

    def test_a_finding_missing_both_fields_is_not_rejected(self):
        # Additive enrichment — a finding lacking root_cause/principle_ref must
        # still count as a valid, blocking finding (not silently dropped).
        out = sanitize_findings({"summary": "x", "findings": [{"title": "real defect"}]})
        assert len(out["findings"]) == 1
