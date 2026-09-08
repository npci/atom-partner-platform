# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the outbound-retry background scheduler."""
import time

import pytest

from app.services import outbound_retry_scheduler


@pytest.fixture(autouse=True)
def _ensure_stopped():
    outbound_retry_scheduler.stop(timeout=2.0)
    yield
    outbound_retry_scheduler.stop(timeout=2.0)


def test_start_and_stop(db_session):
    assert outbound_retry_scheduler.is_running() is False
    outbound_retry_scheduler.start(interval_s=60)
    assert outbound_retry_scheduler.is_running() is True
    outbound_retry_scheduler.stop(timeout=2.0)
    assert outbound_retry_scheduler.is_running() is False


def test_zero_interval_disables_scheduler(db_session):
    outbound_retry_scheduler.start(interval_s=0)
    assert outbound_retry_scheduler.is_running() is False


def test_loop_runs_sweep_at_short_interval(db_session, monkeypatch):
    calls = []

    def _fake_run_sweep(db, **kw):
        calls.append(1)
        return {"delivered": 0, "requeued": 0, "abandoned": 0}

    monkeypatch.setattr("app.services.outbound_retry.run_sweep", _fake_run_sweep)
    outbound_retry_scheduler.start(interval_s=0.05)
    time.sleep(0.3)
    outbound_retry_scheduler.stop(timeout=2.0)
    assert len(calls) >= 1
