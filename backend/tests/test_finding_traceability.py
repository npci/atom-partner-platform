# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the finding-to-fix traceability mechanism (SDLC Gap 8:
docs/ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3)."""
from app.api.dashboard.code import _stamp_finding_ids, _store_generated_files
from app.models import GeneratedCodeFile, IncomingChange


class TestStampFindingIds:
    def test_stamps_an_id_onto_every_finding(self):
        out = {"summary": "x", "findings": [{"file": "a.py", "title": "bug1"}, {"file": "b.py", "title": "bug2"}]}
        result = _stamp_finding_ids(out, "c1", 1, "code_quality")
        assert all(f.get("id", "").startswith("F-") for f in result["findings"])

    def test_same_finding_same_inputs_produces_same_id(self):
        f1 = {"file": "a.py", "title": "bug1"}
        f2 = {"file": "a.py", "title": "bug1"}
        out1 = _stamp_finding_ids({"findings": [f1]}, "c1", 1, "code_quality")
        out2 = _stamp_finding_ids({"findings": [f2]}, "c1", 1, "code_quality")
        assert out1["findings"][0]["id"] == out2["findings"][0]["id"]

    def test_different_change_id_produces_different_id(self):
        f = {"file": "a.py", "title": "bug1"}
        out1 = _stamp_finding_ids({"findings": [dict(f)]}, "c1", 1, "code_quality")
        out2 = _stamp_finding_ids({"findings": [dict(f)]}, "c2", 1, "code_quality")
        assert out1["findings"][0]["id"] != out2["findings"][0]["id"]

    def test_different_iteration_produces_different_id(self):
        f = {"file": "a.py", "title": "bug1"}
        out1 = _stamp_finding_ids({"findings": [dict(f)]}, "c1", 1, "code_quality")
        out2 = _stamp_finding_ids({"findings": [dict(f)]}, "c1", 2, "code_quality")
        assert out1["findings"][0]["id"] != out2["findings"][0]["id"]

    def test_different_reviewer_produces_different_id(self):
        f = {"file": "a.py", "title": "bug1"}
        out1 = _stamp_finding_ids({"findings": [dict(f)]}, "c1", 1, "code_quality")
        out2 = _stamp_finding_ids({"findings": [dict(f)]}, "c1", 1, "security")
        assert out1["findings"][0]["id"] != out2["findings"][0]["id"]

    def test_handles_missing_findings_key(self):
        out = _stamp_finding_ids({"summary": "clean"}, "c1", 1, "code_quality")
        assert out == {"summary": "clean"}

    def test_skips_non_dict_entries_gracefully(self):
        out = _stamp_finding_ids({"findings": ["not a dict"]}, "c1", 1, "code_quality")
        assert out["findings"] == ["not a dict"]


class TestStoreGeneratedFilesFixedFindingRefs:
    def _seed_change(self, db_session, change_id="c1"):
        db_session.add(IncomingChange(id=change_id, npci_change_id=change_id, title="T"))
        db_session.commit()

    def test_iteration_1_has_no_fixed_finding_refs(self, db_session):
        self._seed_change(db_session)
        it = _store_generated_files(db_session, "c1", [{"path": "a.py", "content": "x"}])
        assert it == 1
        row = db_session.query(GeneratedCodeFile).filter_by(change_id="c1", iteration=1).one()
        assert row.fixed_finding_refs is None

    def test_subsequent_iteration_persists_fixed_finding_refs(self, db_session):
        self._seed_change(db_session)
        _store_generated_files(db_session, "c1", [{"path": "a.py", "content": "x"}])
        it = _store_generated_files(
            db_session, "c1", [{"path": "a.py", "content": "y"}],
            fixed_finding_refs=["F-abc123", "F-def456"],
        )
        assert it == 2
        row = db_session.query(GeneratedCodeFile).filter_by(change_id="c1", iteration=2).one()
        assert row.fixed_finding_refs == ["F-abc123", "F-def456"]

    def test_empty_list_stored_as_none(self, db_session):
        self._seed_change(db_session)
        _store_generated_files(db_session, "c1", [{"path": "a.py", "content": "x"}], fixed_finding_refs=[])
        row = db_session.query(GeneratedCodeFile).filter_by(change_id="c1", iteration=1).one()
        assert row.fixed_finding_refs is None

    def test_all_files_in_the_same_iteration_share_the_same_refs(self, db_session):
        self._seed_change(db_session)
        _store_generated_files(db_session, "c1", [{"path": "a.py", "content": "x"}])
        _store_generated_files(
            db_session, "c1",
            [{"path": "a.py", "content": "y"}, {"path": "b.py", "content": "z"}],
            fixed_finding_refs=["F-abc123"],
        )
        rows = db_session.query(GeneratedCodeFile).filter_by(change_id="c1", iteration=2).all()
        assert len(rows) == 2
        assert all(r.fixed_finding_refs == ["F-abc123"] for r in rows)
