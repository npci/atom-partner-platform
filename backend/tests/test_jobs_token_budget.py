# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Integration tests: api/dashboard/jobs.py's budget enforcement at dispatch
(429) and token-usage accumulation onto the AgentJob row (Finding 4)."""
import time

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.dashboard.jobs import _run_job, start_job
from app.config import settings
from app.models import AgentJob


class _NoopBackgroundTasks(BackgroundTasks):
    """Runs the scheduled task SYNCHRONOUSLY (instead of after the response)
    so the test can assert on the job row without a real ASGI server/event
    loop driving BackgroundTasks."""

    def add_task(self, func, *args, **kwargs):
        func(*args, **kwargs)


def _sync_start_job(db_session, *, change_id: str, kind: str, runner):
    bg = _NoopBackgroundTasks()
    return start_job(db_session, bg, change_id=change_id, kind=kind, runner=runner)


def _reload_job(db_session, job_id: str) -> AgentJob:
    """Re-read the job row, bypassing this session's identity map.

    `_run_job` deliberately opens its OWN `SessionLocal` (in production the
    request's session is already closed by the time the background task runs).
    Under the StaticPool test engine both sessions share one connection, so the
    row IS updated — but `db_session` still holds the pre-run copy in its
    identity map and would hand it back unchanged. Since that map holds only
    WEAK references, whether the stale object survives to the assertion depends
    on garbage-collection timing, which made these assertions flaky rather than
    reliably wrong. `expire_all()` forces a fresh SELECT and makes the read
    deterministic.
    """
    db_session.expire_all()
    return db_session.get(AgentJob, job_id)


class TestBudgetEnforcementAtDispatch:
    def test_dispatch_succeeds_under_budget(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "llm_token_budget_per_change", 1000)

        def runner(db, progress):
            pass

        out = _sync_start_job(db_session, change_id="c1", kind="design", runner=runner)
        assert out["status"] in ("done", "running")  # done, since _run_job ran synchronously

    def test_dispatch_rejected_with_429_when_budget_exhausted(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "llm_token_budget_per_change", 1000)
        db_session.add(AgentJob(change_id="c1", kind="code", status="done", tokens_used=1000))
        db_session.commit()

        def runner(db, progress):
            pass

        with pytest.raises(HTTPException) as exc_info:
            _sync_start_job(db_session, change_id="c1", kind="design", runner=runner)
        assert exc_info.value.status_code == 429

    def test_dispatch_unaffected_by_other_changes_budget(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "llm_token_budget_per_change", 1000)
        db_session.add(AgentJob(change_id="c-other", kind="code", status="done", tokens_used=999_999))
        db_session.commit()

        def runner(db, progress):
            pass

        # The point of this test is that dispatch is NOT rejected (no 429) —
        # budget is scoped per change, and "c-other" has exhausted its own.
        out = _sync_start_job(db_session, change_id="c1", kind="design", runner=runner)
        assert _reload_job(db_session, out["job_id"]).status == "done"


class TestTokenAccumulationOnJobRow:
    def test_successful_job_stamps_tokens_used(self, db_session, monkeypatch):
        from app.config import settings as _settings
        monkeypatch.setattr(_settings, "llm_provider", "claude")

        def runner(db, progress):
            from app.core.llm import _LAST_CALL_TOKENS, _TOKEN_ACCUMULATOR
            # Simulate what _record_usage does inside a real call_llm(): add
            # to the active accumulator without needing a real provider call.
            box = _TOKEN_ACCUMULATOR.get()
            box[0] += 123

        out = _sync_start_job(db_session, change_id="c1", kind="design", runner=runner)
        job = _reload_job(db_session, out["job_id"])
        assert job.status == "done"
        assert job.tokens_used == 123

    def test_failed_job_still_stamps_tokens_spent_before_failure(self, db_session):
        def runner(db, progress):
            from app.core.llm import _TOKEN_ACCUMULATOR
            box = _TOKEN_ACCUMULATOR.get()
            box[0] += 77
            raise RuntimeError("boom")

        out = _sync_start_job(db_session, change_id="c1", kind="design", runner=runner)
        job = _reload_job(db_session, out["job_id"])
        assert job.status == "error"
        assert job.tokens_used == 77

    def test_job_with_no_llm_calls_has_zero_tokens_used(self, db_session):
        def runner(db, progress):
            pass

        out = _sync_start_job(db_session, change_id="c1", kind="mr", runner=runner)
        job = _reload_job(db_session, out["job_id"])
        assert job.tokens_used == 0


class TestErrorClassificationOnJobRow:
    def test_failed_job_stamps_error_category_and_code(self, db_session):
        from app.core.errors import GitLabIntegrationError

        def runner(db, progress):
            raise GitLabIntegrationError("token invalid")

        out = _sync_start_job(db_session, change_id="c1", kind="mr", runner=runner)
        job = _reload_job(db_session, out["job_id"])
        assert job.status == "error"
        assert job.error_category == "resource_access"
        assert job.error_code == "gitlab_integration_error"
        assert "token invalid" in job.error

    def test_unclassified_exception_still_gets_a_fallback_category(self, db_session):
        def runner(db, progress):
            raise ValueError("something unexpected")

        out = _sync_start_job(db_session, change_id="c1", kind="mr", runner=runner)
        job = _reload_job(db_session, out["job_id"])
        assert job.error_category == "technical"
        assert job.error_code == "unclassified_error"

    def test_successful_job_has_no_error_classification(self, db_session):
        def runner(db, progress):
            pass

        out = _sync_start_job(db_session, change_id="c1", kind="mr", runner=runner)
        job = _reload_job(db_session, out["job_id"])
        assert job.error_category is None
        assert job.error_code is None


class TestCorrelationIdOnJobRow:
    def test_job_gets_an_auto_generated_correlation_id(self, db_session):
        def runner(db, progress):
            pass

        out = _sync_start_job(db_session, change_id="c1", kind="mr", runner=runner)
        job = _reload_job(db_session, out["job_id"])
        assert job.correlation_id  # non-empty, auto-generated UUID

    def test_runner_sees_its_own_job_correlation_id_as_the_active_context(self, db_session):
        from app.core.correlation import current_correlation_id

        seen = {}

        def runner(db, progress):
            seen["id"] = current_correlation_id()

        out = _sync_start_job(db_session, change_id="c1", kind="mr", runner=runner)
        job = _reload_job(db_session, out["job_id"])
        assert seen["id"] == job.correlation_id
