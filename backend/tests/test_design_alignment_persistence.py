# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The design-alignment result must be PERSISTED, not written to a field that
is wiped moments later.

The check previously reported only through `progress()`, which
`jobs.py::_run_job` sets to None the instant the job completes — so the
deviations were computed, an LLM call was paid for, and the output was erased
before any user could read it.

The second half of this is just as important: persisting it must NOT make it
blocking. `_review_status()` sums `len(content["findings"])` across every
reviewer row, and any non-zero count flips the change to `issues_found` and
gates the MR. Design alignment is advisory by design, so its row carries an
empty `findings` list with the substance in `summary`/`deviations`.
"""
import json

import pytest

from app.api.dashboard import code as code_module
from app.models import CodeReviewReport, GeneratedCodeFile, IncomingChange


@pytest.fixture()
def seeded(db_session):
    db_session.add(IncomingChange(id="c1", npci_change_id="c1", title="T"))
    db_session.add(GeneratedCodeFile(change_id="c1", iteration=1, path="a.py", content="x"))
    db_session.commit()
    return db_session


def _progress(_msg):
    pass


class TestPersistence:
    def test_alignment_result_is_written_to_a_report_row(self, seeded, monkeypatch):
        monkeypatch.setattr(
            code_module, "_latest_code_row",
            lambda db, cid: type("R", (), {"content": json.dumps({"plan_markdown": "plan"})})(),
        )
        monkeypatch.setattr(code_module, "_latest_generated_files", lambda db, cid: [{"path": "a.py", "content": "x"}])
        monkeypatch.setattr(
            "app.agents.design_alignment.check_alignment",
            lambda plan, files: {"aligned": False, "deviations": ["added an undocumented endpoint"]},
        )

        code_module._check_design_alignment_if_clean(
            "c1", seeded, _progress, {"status": "clean", "reviewed_iteration": 1},
        )

        row = seeded.query(CodeReviewReport).filter_by(reviewer="design_alignment").one()
        content = json.loads(row.content)
        assert content["aligned"] is False
        assert content["deviations"] == ["added an undocumented endpoint"]
        assert "1 deviation" in content["summary"]

    def test_persisted_row_never_blocks_the_merge_request(self, seeded, monkeypatch):
        """The critical safety property: an advisory row must not flip the
        change to issues_found."""
        monkeypatch.setattr(
            code_module, "_latest_code_row",
            lambda db, cid: type("R", (), {"content": json.dumps({"plan_markdown": "plan"})})(),
        )
        monkeypatch.setattr(code_module, "_latest_generated_files", lambda db, cid: [{"path": "a.py", "content": "x"}])
        monkeypatch.setattr(
            "app.agents.design_alignment.check_alignment",
            lambda plan, files: {"aligned": False, "deviations": ["d1", "d2", "d3"]},
        )

        code_module._check_design_alignment_if_clean(
            "c1", seeded, _progress, {"status": "clean", "reviewed_iteration": 1},
        )

        row = seeded.query(CodeReviewReport).filter_by(reviewer="design_alignment").one()
        assert json.loads(row.content)["findings"] == [], (
            "a non-empty findings list would turn this advisory lens into an MR blocker"
        )
        st = code_module._review_status(seeded, "c1")
        assert st["findings_count"] == 0
        assert st["status"] == "clean"

    def test_repeat_runs_upsert_rather_than_duplicate(self, seeded, monkeypatch):
        monkeypatch.setattr(
            code_module, "_latest_code_row",
            lambda db, cid: type("R", (), {"content": json.dumps({"plan_markdown": "plan"})})(),
        )
        monkeypatch.setattr(code_module, "_latest_generated_files", lambda db, cid: [{"path": "a.py", "content": "x"}])
        monkeypatch.setattr(
            "app.agents.design_alignment.check_alignment",
            lambda plan, files: {"aligned": True, "deviations": []},
        )
        for _ in range(3):
            code_module._check_design_alignment_if_clean(
                "c1", seeded, _progress, {"status": "clean", "reviewed_iteration": 1},
            )
        assert seeded.query(CodeReviewReport).filter_by(reviewer="design_alignment").count() == 1


class TestNoSignalIsNotRecordedAsAgreement:
    def test_aligned_none_writes_no_row(self, seeded, monkeypatch):
        """`aligned=None` means the check could not run (no LLM key, provider
        error). Recording that as 'no deviations' would assert agreement nobody
        verified."""
        monkeypatch.setattr(
            code_module, "_latest_code_row",
            lambda db, cid: type("R", (), {"content": json.dumps({"plan_markdown": "plan"})})(),
        )
        monkeypatch.setattr(code_module, "_latest_generated_files", lambda db, cid: [{"path": "a.py", "content": "x"}])
        monkeypatch.setattr(
            "app.agents.design_alignment.check_alignment",
            lambda plan, files: {"aligned": None, "deviations": []},
        )
        code_module._check_design_alignment_if_clean(
            "c1", seeded, _progress, {"status": "clean", "reviewed_iteration": 1},
        )
        assert seeded.query(CodeReviewReport).filter_by(reviewer="design_alignment").count() == 0


class TestGuards:
    def test_skipped_when_review_is_not_clean(self, seeded, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            "app.agents.design_alignment.check_alignment",
            lambda plan, files: called.update(n=called["n"] + 1) or {"aligned": True, "deviations": []},
        )
        code_module._check_design_alignment_if_clean(
            "c1", seeded, _progress, {"status": "issues_found", "reviewed_iteration": 1},
        )
        assert called["n"] == 0
        assert seeded.query(CodeReviewReport).count() == 0

    def test_check_failure_never_raises(self, seeded, monkeypatch):
        monkeypatch.setattr(
            code_module, "_latest_code_row",
            lambda db, cid: type("R", (), {"content": json.dumps({"plan_markdown": "plan"})})(),
        )
        monkeypatch.setattr(code_module, "_latest_generated_files", lambda db, cid: [{"path": "a.py", "content": "x"}])
        monkeypatch.setattr(
            "app.agents.design_alignment.check_alignment",
            lambda plan, files: (_ for _ in ()).throw(RuntimeError("provider down")),
        )
        code_module._check_design_alignment_if_clean(
            "c1", seeded, _progress, {"status": "clean", "reviewed_iteration": 1},
        )  # must not raise
