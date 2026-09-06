# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the auto-fix loop's non-regression check (SDLC Gap 5:
docs/ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3).

Unit-tests `_run_fix_job`'s control flow by monkeypatching its module-level
dependencies (`_review_status`, `_reviewed_findings`, `_findings_map_to_files`,
`fix_code_files`, `_store_generated_files`, `_review_current_iteration`,
`_latest_generated_files`) rather than exercising the real LLM-backed review
agents — those return mock (empty) findings without an LLM key configured,
which would make the loop converge immediately regardless of the regression
logic under test.

`_review_status()` is called:
  1. ONCE before the loop (the initial "is there anything to fix" check),
  2. ONCE PER LOOP ITERATION thereafter,
  3. ONCE MORE after the loop exits (the closing "final" status used to
     report converged-vs-stopped).
The status iterators below are sized to match that exact call sequence.
"""
import pytest

from app.agents import code_files as code_files_module
from app.api.dashboard import code as code_module
from app.config import settings
from app.models import IncomingChange


def _seed_change(db_session, change_id="c1"):
    db_session.add(IncomingChange(id=change_id, npci_change_id=change_id, title="Test change"))
    db_session.commit()


def _noop_progress(msg):
    pass


def _status(status: str, findings_count: int, reviewed_iteration: int = 1) -> dict:
    return {"status": status, "findings_count": findings_count, "reviewed_iteration": reviewed_iteration}


def _patch_common(monkeypatch, statuses: list[dict]):
    it = iter(statuses)
    monkeypatch.setattr(code_module, "_review_status", lambda db, cid: next(it))
    monkeypatch.setattr(code_module, "_reviewed_findings", lambda db, cid, iteration: [{"file": "a.py", "title": "x"}])
    monkeypatch.setattr(code_module, "_findings_map_to_files", lambda findings, files: True)
    monkeypatch.setattr(code_module, "_latest_generated_files", lambda db, cid: [{"path": "a.py", "content": "x"}])
    monkeypatch.setattr(
        code_module, "_store_generated_files",
        lambda db, cid, files, *, fixed_finding_refs=None: 2,
    )
    monkeypatch.setattr(code_module, "_review_current_iteration", lambda cid, db, progress: None)
    # `fix_code_files` is imported *inside* `_run_fix_job` (a deferred import,
    # to keep the heavy agent module off the dashboard import path), so it is
    # never an attribute of `code_module`. Patch it at its definition site —
    # the function-local `from ... import` re-reads it on every call.
    monkeypatch.setattr(code_files_module, "fix_code_files", lambda **kw: [{"path": "a.py", "content": "y"}])
    monkeypatch.setattr(code_module, "_latest_design_summary", lambda db, cid: None)
    monkeypatch.setattr(code_module, "_latest_indexed_repo", lambda db: None)
    return it


class TestNonRegressionCheck:
    def test_loop_stops_when_finding_count_does_not_decrease(self, db_session, monkeypatch):
        """Findings count flat across rounds (5 -> 5) — the loop must stop
        itself rather than burn every round to the cap.

        Call sequence: initial(5) -> loop-iter1(5, prev=None->set 5, fix
        round1) -> loop-iter2(5, 5>=5 -> break) -> final(5)."""
        _seed_change(db_session)
        monkeypatch.setattr(settings, "code_review_max_fix_rounds", 5)
        progress_messages = []
        _patch_common(monkeypatch, [
            _status("issues_found", 5),  # initial check
            _status("issues_found", 5),  # loop iter 1
            _status("issues_found", 5),  # loop iter 2 — flat, stop
            _status("issues_found", 5),  # final status (post-loop)
        ])

        code_module._run_fix_job("c1", db_session, progress_messages.append)

        assert any("possible oscillation" in m for m in progress_messages)
        fix_rounds = [m for m in progress_messages if m.startswith("round") and "fixing" in m]
        assert len(fix_rounds) == 1  # only round 1 ran before the flat-count stop

    def test_loop_continues_when_finding_count_decreases(self, db_session, monkeypatch):
        """Findings count strictly decreasing (5 -> 3 -> clean) — the loop
        must run all rounds through to convergence, not stop early.

        Call sequence: initial(5) -> loop-iter1(5, fix round1) ->
        loop-iter2(3, improved, fix round2) -> loop-iter3(clean -> break) ->
        final(clean)."""
        _seed_change(db_session)
        monkeypatch.setattr(settings, "code_review_max_fix_rounds", 5)
        progress_messages = []
        _patch_common(monkeypatch, [
            _status("issues_found", 5),  # initial check
            _status("issues_found", 5),  # loop iter 1
            _status("issues_found", 3),  # loop iter 2 — improved
            _status("clean", 0),         # loop iter 3 — converged, break
            _status("clean", 0),         # final status (post-loop)
        ])

        code_module._run_fix_job("c1", db_session, progress_messages.append)

        assert not any("possible oscillation" in m for m in progress_messages)
        assert any("converged" in m for m in progress_messages)
        fix_rounds = [m for m in progress_messages if m.startswith("round") and "fixing" in m]
        assert len(fix_rounds) == 2

    def test_loop_stops_when_finding_count_increases(self, db_session, monkeypatch):
        """A round that makes things WORSE (3 -> 5) must also be treated as
        non-progress and stop the loop — not just a flat count.

        Call sequence: initial(3) -> loop-iter1(3, fix round1) ->
        loop-iter2(5, worse -> break) -> final(5)."""
        _seed_change(db_session)
        monkeypatch.setattr(settings, "code_review_max_fix_rounds", 5)
        progress_messages = []
        _patch_common(monkeypatch, [
            _status("issues_found", 3),  # initial check
            _status("issues_found", 3),  # loop iter 1
            _status("issues_found", 5),  # loop iter 2 — got worse, stop
            _status("issues_found", 5),  # final status (post-loop)
        ])

        code_module._run_fix_job("c1", db_session, progress_messages.append)

        assert any("possible oscillation" in m for m in progress_messages)
        fix_rounds = [m for m in progress_messages if m.startswith("round") and "fixing" in m]
        assert len(fix_rounds) == 1

    def test_loop_respects_round_cap_when_always_improving_but_never_reaching_zero(self, db_session, monkeypatch):
        """A steadily-but-slowly improving sequence that never quite reaches
        'clean' within the round cap must still stop at the cap (pre-existing
        behavior, unaffected by the new regression check).

        Call sequence (cap=2): initial(5) -> loop-iter1(5, fix round1) ->
        loop-iter2(4, improved, fix round2) -> [rounds==cap, loop exits] ->
        final(3)."""
        _seed_change(db_session)
        monkeypatch.setattr(settings, "code_review_max_fix_rounds", 2)
        progress_messages = []
        _patch_common(monkeypatch, [
            _status("issues_found", 5),  # initial check
            _status("issues_found", 5),  # loop iter 1
            _status("issues_found", 4),  # loop iter 2
            _status("issues_found", 3),  # final status (post-loop, cap reached)
        ])

        code_module._run_fix_job("c1", db_session, progress_messages.append)

        fix_rounds = [m for m in progress_messages if m.startswith("round") and "fixing" in m]
        assert len(fix_rounds) == 2
        assert any("stopped after 2 round" in m for m in progress_messages)


class TestExistingBehaviorPreserved:
    def test_raises_if_no_issues_found_at_start(self, db_session, monkeypatch):
        _seed_change(db_session)
        _patch_common(monkeypatch, [_status("clean", 0)])
        with pytest.raises(RuntimeError, match="no review findings to fix"):
            code_module._run_fix_job("c1", db_session, _noop_progress)

    def test_raises_for_unknown_change(self, db_session):
        with pytest.raises(RuntimeError, match="unknown change_id"):
            code_module._run_fix_job("does-not-exist", db_session, _noop_progress)

    def test_stops_when_findings_do_not_map_to_files(self, db_session, monkeypatch):
        """Call sequence: initial(2) -> loop-iter1(2, prev=None->set 2, then
        _findings_map_to_files=False -> break) -> final(2)."""
        _seed_change(db_session)
        progress_messages = []
        _patch_common(monkeypatch, [
            _status("issues_found", 2),  # initial check
            _status("issues_found", 2),  # loop iter 1
            _status("issues_found", 2),  # final status (post-loop)
        ])
        monkeypatch.setattr(code_module, "_findings_map_to_files", lambda findings, files: False)

        code_module._run_fix_job("c1", db_session, progress_messages.append)
        assert any("don't map to a generated file" in m for m in progress_messages)
