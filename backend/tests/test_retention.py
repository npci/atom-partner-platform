# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the data retention/purge policy (Finding 7:
security_architecture_skills.md §10.3, EA_Skills.md P6)."""
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models import AgentRun, GeneratedCodeFile
from app.services.retention import (
    purge_stale_agent_run_payloads,
    purge_superseded_generated_code_files,
    run_all,
)


def _add_files(db_session, change_id: str, iteration: int, n: int = 2):
    for i in range(n):
        db_session.add(GeneratedCodeFile(
            change_id=change_id, iteration=iteration,
            path=f"src/file{i}.py", content="x" * 10,
        ))
    db_session.commit()


class TestPurgeSupersededGeneratedCodeFiles:
    def test_keeps_only_latest_n_iterations(self, db_session, monkeypatch):
        for it in range(1, 6):  # iterations 1..5
            _add_files(db_session, "c1", it)
        deleted = purge_superseded_generated_code_files(db_session, keep_latest_n=3)
        remaining_iterations = sorted({
            r.iteration for r in db_session.query(GeneratedCodeFile).filter_by(change_id="c1").all()
        })
        assert remaining_iterations == [3, 4, 5]
        assert deleted == 4  # iterations 1, 2 x 2 files each

    def test_current_iteration_never_touched_when_under_limit(self, db_session):
        _add_files(db_session, "c1", 1)
        deleted = purge_superseded_generated_code_files(db_session, keep_latest_n=3)
        assert deleted == 0
        assert db_session.query(GeneratedCodeFile).filter_by(change_id="c1").count() == 2

    def test_different_changes_are_independent(self, db_session):
        for it in range(1, 5):
            _add_files(db_session, "c1", it)
        _add_files(db_session, "c2", 1)
        purge_superseded_generated_code_files(db_session, keep_latest_n=2)
        assert sorted({
            r.iteration for r in db_session.query(GeneratedCodeFile).filter_by(change_id="c1").all()
        }) == [3, 4]
        assert db_session.query(GeneratedCodeFile).filter_by(change_id="c2").count() == 2  # untouched

    def test_zero_keep_latest_n_disables_purge(self, db_session):
        for it in range(1, 5):
            _add_files(db_session, "c1", it)
        deleted = purge_superseded_generated_code_files(db_session, keep_latest_n=0)
        assert deleted == 0
        assert db_session.query(GeneratedCodeFile).filter_by(change_id="c1").count() == 8

    def test_reads_default_from_settings_when_unspecified(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "retention_keep_latest_iterations", 1)
        for it in range(1, 4):
            _add_files(db_session, "c1", it)
        purge_superseded_generated_code_files(db_session)  # no explicit keep_latest_n
        assert sorted({
            r.iteration for r in db_session.query(GeneratedCodeFile).filter_by(change_id="c1").all()
        }) == [3]


class TestPurgeStaleAgentRunPayloads:
    def _add_run(self, db_session, completed_at, payload):
        run = AgentRun(
            agent_name="design", status="succeeded",
            completed_at=completed_at, result_payload=payload,
        )
        db_session.add(run)
        db_session.commit()
        return run

    def test_clears_payload_past_retention_window(self, db_session):
        old = self._add_run(
            db_session,
            datetime.now(timezone.utc) - timedelta(days=120),
            {"foo": "bar"},
        )
        cleared = purge_stale_agent_run_payloads(db_session, older_than_days=90)
        db_session.refresh(old)
        assert cleared == 1
        assert old.result_payload is None
        # The row itself (status, timestamps) is preserved.
        assert old.status == "succeeded"

    def test_leaves_recent_runs_untouched(self, db_session):
        recent = self._add_run(
            db_session,
            datetime.now(timezone.utc) - timedelta(days=5),
            {"foo": "bar"},
        )
        cleared = purge_stale_agent_run_payloads(db_session, older_than_days=90)
        db_session.refresh(recent)
        assert cleared == 0
        assert recent.result_payload == {"foo": "bar"}

    def test_zero_days_disables_purge(self, db_session):
        old = self._add_run(
            db_session,
            datetime.now(timezone.utc) - timedelta(days=365),
            {"foo": "bar"},
        )
        cleared = purge_stale_agent_run_payloads(db_session, older_than_days=0)
        db_session.refresh(old)
        assert cleared == 0
        assert old.result_payload == {"foo": "bar"}


class TestRunAll:
    def test_returns_summary_dict(self, db_session):
        summary = run_all(db_session)
        assert set(summary.keys()) == {"generated_code_files_purged", "agent_run_payloads_cleared"}
        assert all(isinstance(v, int) for v in summary.values())
