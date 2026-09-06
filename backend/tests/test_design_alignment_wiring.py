# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for _check_design_alignment_if_clean's wiring into the review/fix
job runners (SDLC Gap 6)."""
import json

from app.api.dashboard import code as code_module
from app.models import CodeReport, IncomingChange


def _seed_change_and_plan(db_session, change_id="c1"):
    db_session.add(IncomingChange(id=change_id, npci_change_id=change_id, title="T"))
    db_session.add(CodeReport(change_id=change_id, version=1, content=json.dumps({
        "plan_markdown": "do the thing", "work_items": [],
    })))
    db_session.commit()


class TestCheckDesignAlignmentIfClean:
    def test_skips_when_status_is_not_clean(self, db_session, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(
            "app.agents.design_alignment.check_alignment",
            lambda *a, **kw: called.update(n=called["n"] + 1),
        )
        messages = []
        code_module._check_design_alignment_if_clean(
            "c1", db_session, messages.append, {"status": "issues_found"},
        )
        assert called["n"] == 0
        assert messages == []

    def test_runs_and_reports_deviations_when_clean(self, db_session, monkeypatch):
        _seed_change_and_plan(db_session)
        monkeypatch.setattr(
            code_module, "_latest_generated_files",
            lambda db, cid: [{"path": "a.py", "content": "x"}],
        )
        monkeypatch.setattr(
            "app.agents.design_alignment.check_alignment",
            lambda plan_markdown, files: {"aligned": False, "deviations": ["missing file X"]},
        )
        messages = []
        code_module._check_design_alignment_if_clean(
            "c1", db_session, messages.append, {"status": "clean"},
        )
        assert any("deviation" in m for m in messages)

    def test_runs_and_reports_no_deviations_when_aligned(self, db_session, monkeypatch):
        _seed_change_and_plan(db_session)
        monkeypatch.setattr(
            code_module, "_latest_generated_files",
            lambda db, cid: [{"path": "a.py", "content": "x"}],
        )
        monkeypatch.setattr(
            "app.agents.design_alignment.check_alignment",
            lambda plan_markdown, files: {"aligned": True, "deviations": []},
        )
        messages = []
        code_module._check_design_alignment_if_clean(
            "c1", db_session, messages.append, {"status": "clean"},
        )
        assert any("no deviations" in m for m in messages)

    def test_silent_when_alignment_check_could_not_run(self, db_session, monkeypatch):
        _seed_change_and_plan(db_session)
        monkeypatch.setattr(
            code_module, "_latest_generated_files",
            lambda db, cid: [{"path": "a.py", "content": "x"}],
        )
        monkeypatch.setattr(
            "app.agents.design_alignment.check_alignment",
            lambda plan_markdown, files: {"aligned": None, "deviations": []},
        )
        messages = []
        code_module._check_design_alignment_if_clean(
            "c1", db_session, messages.append, {"status": "clean"},
        )
        # "no signal" -> no extra progress message beyond whatever the caller
        # already emitted before invoking this function.
        assert messages == ["review is clean — checking design alignment"]

    def test_no_plan_row_does_not_raise(self, db_session):
        db_session.add(IncomingChange(id="c1", npci_change_id="c1", title="T"))
        db_session.commit()
        messages = []
        # Must not raise even though no CodeReport exists for this change.
        code_module._check_design_alignment_if_clean(
            "c1", db_session, messages.append, {"status": "clean"},
        )

    def test_exception_in_alignment_check_is_swallowed(self, db_session, monkeypatch):
        _seed_change_and_plan(db_session)
        monkeypatch.setattr(
            code_module, "_latest_generated_files",
            lambda db, cid: [{"path": "a.py", "content": "x"}],
        )

        def _boom(*a, **kw):
            raise RuntimeError("unexpected failure")

        monkeypatch.setattr("app.agents.design_alignment.check_alignment", _boom)
        messages = []
        # Must not raise — this check is advisory-only.
        code_module._check_design_alignment_if_clean(
            "c1", db_session, messages.append, {"status": "clean"},
        )
