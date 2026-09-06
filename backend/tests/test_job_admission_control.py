# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Admission control on agent jobs: the cross-change concurrency bulkhead and
the shutdown drain gate (EA_Skills.md P2/P3).

The `agent_job_dispatch` boundary was declared in core/hostility.py from the
start but never enforced — these tests exist to make sure it actually is.
"""
import threading
import time

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.dashboard.jobs import _run_job, start_job
from app.core import hostility, resilience, runtime
from app.models import AgentJob


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    runtime.resume_accepting_for_tests()
    resilience.reset_for_tests()
    yield
    runtime.resume_accepting_for_tests()
    resilience.reset_for_tests()


class _SyncBackgroundTasks(BackgroundTasks):
    """Run the scheduled task synchronously so assertions can inspect the row."""

    def add_task(self, func, *args, **kwargs):
        func(*args, **kwargs)


class _DeferredBackgroundTasks(BackgroundTasks):
    """Capture the task instead of running it, so the test controls timing."""

    def __init__(self):
        super().__init__()
        self.captured = []

    def add_task(self, func, *args, **kwargs):
        self.captured.append((func, args, kwargs))


def _reload(db, job_id):
    db.expire_all()
    return db.get(AgentJob, job_id)


class TestDrainGateAtDispatch:
    def test_start_job_rejected_with_503_while_draining(self, db_session):
        runtime.stop_accepting()
        with pytest.raises(HTTPException) as exc:
            start_job(
                db_session, _SyncBackgroundTasks(),
                change_id="c1", kind="design", runner=lambda db, p: None,
            )
        assert exc.value.status_code == 503
        assert "shutting down" in str(exc.value.detail).lower()

    def test_no_job_row_is_left_behind_by_a_rejected_dispatch(self, db_session):
        """A rejected dispatch must not leave a phantom row the UI would show
        as a job that never runs."""
        runtime.stop_accepting()
        with pytest.raises(HTTPException):
            start_job(
                db_session, _SyncBackgroundTasks(),
                change_id="c1", kind="design", runner=lambda db, p: None,
            )
        assert db_session.query(AgentJob).count() == 0

    def test_job_marked_cleanly_if_drain_begins_before_execution(self, db_session, monkeypatch):
        """Window between the 202 and the background task actually starting."""
        bg = _DeferredBackgroundTasks()
        out = start_job(db_session, bg, change_id="c1", kind="design", runner=lambda db, p: None)

        runtime.stop_accepting()  # drain starts before the task runs
        func, args, kwargs = bg.captured[0]
        func(*args, **kwargs)

        job = _reload(db_session, out["job_id"])
        assert job.status == "error"
        assert job.error_code == "job_not_admitted"
        assert job.error_category == "capacity"


class TestConcurrencyBulkhead:
    def test_bulkhead_is_actually_enforced(self, db_session, monkeypatch):
        """Cap concurrency at 1, hold one job open, and confirm a second is
        rejected rather than running anyway."""
        monkeypatch.setattr(hostility.settings, "agentic_max_concurrent_runs", 1)
        hostility.validate_at_startup()  # rebuild BOUNDARIES from the new setting
        resilience.reset_for_tests()
        monkeypatch.setattr(
            __import__("app.config", fromlist=["settings"]).settings,
            "agent_job_bulkhead_timeout_s", 0.2,
        )

        release = threading.Event()
        started = threading.Event()

        def slow_runner(db, progress):
            started.set()
            release.wait(timeout=5)

        bg1 = _DeferredBackgroundTasks()
        first = start_job(db_session, bg1, change_id="c1", kind="design", runner=slow_runner)
        func, args, kwargs = bg1.captured[0]
        t = threading.Thread(target=func, args=args, kwargs=kwargs, daemon=True)
        t.start()
        assert started.wait(timeout=5), "first job never started"

        # Second job: the single permit is held, so it must be refused.
        bg2 = _DeferredBackgroundTasks()
        second = start_job(db_session, bg2, change_id="c2", kind="design", runner=lambda db, p: None)
        f2, a2, k2 = bg2.captured[0]
        f2(*a2, **k2)

        job2 = _reload(db_session, second["job_id"])
        assert job2.status == "error"
        assert job2.error_code == "job_not_admitted"
        assert "concurrent-job limit" in job2.error

        release.set()
        t.join(timeout=5)

    def test_permit_is_released_after_a_job_finishes(self, db_session, monkeypatch):
        """A leaked permit would permanently shrink the cap, so run more jobs
        sequentially than the cap allows and confirm all succeed."""
        monkeypatch.setattr(hostility.settings, "agentic_max_concurrent_runs", 1)
        hostility.validate_at_startup()
        resilience.reset_for_tests()

        for i in range(4):
            out = start_job(
                db_session, _SyncBackgroundTasks(),
                change_id=f"c{i}", kind="design", runner=lambda db, p: None,
            )
            job = _reload(db_session, out["job_id"])
            assert job.status == "done", f"job {i} was refused — a permit leaked"

    def test_permit_released_even_when_the_runner_raises(self, db_session, monkeypatch):
        monkeypatch.setattr(hostility.settings, "agentic_max_concurrent_runs", 1)
        hostility.validate_at_startup()
        resilience.reset_for_tests()

        def boom(db, progress):
            raise RuntimeError("boom")

        first = start_job(db_session, _SyncBackgroundTasks(), change_id="c1", kind="design", runner=boom)
        assert _reload(db_session, first["job_id"]).status == "error"

        second = start_job(
            db_session, _SyncBackgroundTasks(),
            change_id="c2", kind="design", runner=lambda db, p: None,
        )
        assert _reload(db_session, second["job_id"]).status == "done", "permit leaked on the error path"


class TestRegistryIsCleanedUp:
    def test_inflight_registry_empty_after_a_job_completes(self, db_session):
        start_job(
            db_session, _SyncBackgroundTasks(),
            change_id="c1", kind="design", runner=lambda db, p: None,
        )
        assert runtime.inflight_count() == 0

    def test_inflight_registry_empty_after_a_job_fails(self, db_session):
        def boom(db, progress):
            raise RuntimeError("boom")

        start_job(db_session, _SyncBackgroundTasks(), change_id="c1", kind="design", runner=boom)
        assert runtime.inflight_count() == 0, "a leaked entry would stall every future drain"
