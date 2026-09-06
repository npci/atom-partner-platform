# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for core/runtime.py — the single-instance guard (EA_Skills.md P2)
and graceful drain of in-flight agent jobs (P3)."""
import threading
import time

import pytest

from app.config import settings
from app.core import runtime


@pytest.fixture(autouse=True)
def _reset_runtime():
    runtime.resume_accepting_for_tests()
    yield
    runtime.resume_accepting_for_tests()


class TestWorkerCountDetection:
    def test_no_env_means_single_worker(self, monkeypatch):
        for var in runtime._WORKER_COUNT_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        assert runtime.detect_worker_count() == (1, None)

    @pytest.mark.parametrize("var", ["WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"])
    def test_each_known_env_var_is_detected(self, monkeypatch, var):
        for v in runtime._WORKER_COUNT_ENV_VARS:
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv(var, "4")
        assert runtime.detect_worker_count() == (4, var)

    def test_explicit_single_worker_is_not_flagged(self, monkeypatch):
        monkeypatch.setenv("WEB_CONCURRENCY", "1")
        assert runtime.detect_worker_count() == (1, None)

    def test_garbage_value_is_ignored_not_raised(self, monkeypatch):
        """A malformed env var must not be the thing that takes the service
        down — it degrades to 'assume single worker'."""
        for v in runtime._WORKER_COUNT_ENV_VARS:
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("WEB_CONCURRENCY", "not-a-number")
        assert runtime.detect_worker_count() == (1, None)


class TestSingleInstanceValidation:
    def test_single_worker_passes(self, monkeypatch):
        for v in runtime._WORKER_COUNT_ENV_VARS:
            monkeypatch.delenv(v, raising=False)
        runtime.validate_single_instance()  # must not raise

    def test_multi_worker_refuses_to_boot(self, monkeypatch):
        monkeypatch.setenv("WEB_CONCURRENCY", "4")
        monkeypatch.setattr(settings, "partner_allow_multi_worker", False)
        with pytest.raises(RuntimeError) as exc:
            runtime.validate_single_instance()
        msg = str(exc.value)
        # The error must explain the CONSEQUENCE and both remedies, not just
        # state that the check failed.
        assert "rate limit" in msg.lower()
        assert "PARTNER_ALLOW_MULTI_WORKER" in msg
        assert "WEB_CONCURRENCY" in msg

    def test_explicit_override_allows_multi_worker(self, monkeypatch):
        monkeypatch.setenv("WEB_CONCURRENCY", "4")
        monkeypatch.setattr(settings, "partner_allow_multi_worker", True)
        runtime.validate_single_instance()  # warns, does not raise


class TestInflightRegistry:
    def test_register_and_unregister(self):
        assert runtime.inflight_count() == 0
        runtime.register_job("j1")
        runtime.register_job("j2")
        assert runtime.inflight_count() == 2
        assert runtime.inflight_job_ids() == ["j1", "j2"]
        runtime.unregister_job("j1")
        assert runtime.inflight_job_ids() == ["j2"]

    def test_unregister_is_idempotent(self):
        """Callers run it from a `finally`, which can fire on paths where
        registration never happened."""
        runtime.unregister_job("never-registered")
        runtime.register_job("j1")
        runtime.unregister_job("j1")
        runtime.unregister_job("j1")
        assert runtime.inflight_count() == 0

    def test_register_refused_once_draining(self):
        runtime.stop_accepting()
        with pytest.raises(runtime.ShuttingDownError):
            runtime.register_job("late-job")
        assert runtime.inflight_count() == 0


class TestDrain:
    def test_drain_returns_immediately_when_idle(self):
        remaining, elapsed = runtime.drain(timeout_s=5)
        assert remaining == 0
        assert elapsed < 1.0
        assert not runtime.is_accepting()

    def test_drain_stops_accepting_new_work(self):
        runtime.drain(timeout_s=0.1)
        assert not runtime.is_accepting()
        with pytest.raises(runtime.ShuttingDownError):
            runtime.register_job("after-drain")

    def test_drain_waits_for_inflight_job_to_finish(self):
        runtime.register_job("slow")

        def finish_soon():
            time.sleep(0.3)
            runtime.unregister_job("slow")

        threading.Thread(target=finish_soon, daemon=True).start()
        remaining, elapsed = runtime.drain(timeout_s=5, poll_interval_s=0.05)
        assert remaining == 0, "drain should have waited for the job to finish"
        assert elapsed >= 0.25

    def test_drain_times_out_and_reports_stragglers(self):
        runtime.register_job("stuck")
        remaining, elapsed = runtime.drain(timeout_s=0.3, poll_interval_s=0.05)
        assert remaining == 1
        assert elapsed >= 0.3

    def test_zero_timeout_disables_waiting(self):
        runtime.register_job("stuck")
        remaining, elapsed = runtime.drain(timeout_s=0)
        assert remaining == 1
        assert elapsed == 0.0
