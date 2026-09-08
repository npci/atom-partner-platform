# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the retention background scheduler (services/retention_scheduler.py)."""
import time

import pytest

from app.services import retention_scheduler


@pytest.fixture(autouse=True)
def _ensure_stopped():
    retention_scheduler.stop(timeout=2.0)
    yield
    retention_scheduler.stop(timeout=2.0)


def test_start_and_stop(db_session):
    assert retention_scheduler.is_running() is False
    retention_scheduler.start(interval_s=60)
    assert retention_scheduler.is_running() is True
    retention_scheduler.stop(timeout=2.0)
    assert retention_scheduler.is_running() is False


def test_start_is_idempotent(db_session):
    retention_scheduler.start(interval_s=60)
    first_thread = retention_scheduler._thread
    retention_scheduler.start(interval_s=60)  # no-op — already running
    assert retention_scheduler._thread is first_thread
    retention_scheduler.stop(timeout=2.0)


def test_zero_interval_disables_scheduler(db_session):
    retention_scheduler.start(interval_s=0)
    assert retention_scheduler.is_running() is False


def test_stop_without_start_is_safe():
    retention_scheduler.stop(timeout=1.0)  # must not raise


def test_loop_runs_sweep_at_short_interval(db_session, monkeypatch):
    """With a very short interval, the loop should invoke run_all() at least
    once within a couple of seconds without needing a full day's wait."""
    calls = []

    def _fake_run_all(db):
        calls.append(1)
        return {"generated_code_files_purged": 0, "agent_run_payloads_cleared": 0}

    monkeypatch.setattr("app.services.retention.run_all", _fake_run_all)
    retention_scheduler.start(interval_s=0.05)
    time.sleep(0.3)
    retention_scheduler.stop(timeout=2.0)
    assert len(calls) >= 1
