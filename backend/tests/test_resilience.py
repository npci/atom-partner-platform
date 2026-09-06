# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the circuit breaker + bulkhead primitives
(docs/adr/ADR-0001-llm-circuit-breaker-and-bulkhead.md).
"""
import time

import pytest

from app.core.resilience import (
    Bulkhead,
    CircuitBreaker,
    CircuitOpenError,
    breaker_for,
    bulkhead_for,
    reset_for_tests,
)


class TestCircuitBreaker:
    def test_closed_by_default(self):
        cb = CircuitBreaker("t1", failure_threshold=3, cooldown_s=10)
        assert cb.state == "closed"

    def test_opens_after_threshold_consecutive_failures(self):
        cb = CircuitBreaker("t2", failure_threshold=3, cooldown_s=10)
        for _ in range(3):
            with pytest.raises(ValueError):
                with cb.call():
                    raise ValueError("boom")
        assert cb.state == "open"

    def test_open_circuit_rejects_without_calling(self):
        cb = CircuitBreaker("t3", failure_threshold=1, cooldown_s=10)
        with pytest.raises(ValueError):
            with cb.call():
                raise ValueError("boom")
        calls = []
        with pytest.raises(CircuitOpenError):
            with cb.call():
                calls.append(1)  # must never execute
        assert calls == []

    def test_half_open_after_cooldown_then_closes_on_success(self):
        cb = CircuitBreaker("t4", failure_threshold=1, cooldown_s=0.1)
        with pytest.raises(ValueError):
            with cb.call():
                raise ValueError("boom")
        assert cb.state == "open"
        time.sleep(0.15)
        assert cb.state == "half_open"
        with cb.call():
            pass  # success
        assert cb.state == "closed"

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker("t5", failure_threshold=1, cooldown_s=0.1)
        with pytest.raises(ValueError):
            with cb.call():
                raise ValueError("boom")
        time.sleep(0.15)
        assert cb.state == "half_open"
        with pytest.raises(ValueError):
            with cb.call():
                raise ValueError("boom again")
        assert cb.state == "open"

    def test_success_resets_consecutive_failure_count(self):
        cb = CircuitBreaker("t6", failure_threshold=3, cooldown_s=10)
        with pytest.raises(ValueError):
            with cb.call():
                raise ValueError("boom")
        with cb.call():
            pass  # success resets the counter
        assert cb._consecutive_failures == 0
        assert cb.state == "closed"


class TestBulkhead:
    def test_allows_up_to_max_concurrent(self):
        bh = Bulkhead("b1", max_concurrent=2)
        with bh.acquire(timeout=1.0):
            with bh.acquire(timeout=1.0):
                pass  # both fit

    def test_rejects_beyond_max_concurrent(self):
        bh = Bulkhead("b2", max_concurrent=1)
        with bh.acquire(timeout=1.0):
            with pytest.raises(RuntimeError, match="saturated"):
                with bh.acquire(timeout=0.05):
                    pass

    def test_releases_on_exit_allowing_reacquire(self):
        bh = Bulkhead("b3", max_concurrent=1)
        with bh.acquire(timeout=1.0):
            pass
        with bh.acquire(timeout=1.0):  # slot freed, must succeed
            pass


class TestRegistryHelpers:
    def setup_method(self):
        reset_for_tests()

    def teardown_method(self):
        reset_for_tests()

    def test_breaker_for_reads_hostility_config(self):
        cb = breaker_for("llm_provider")
        assert cb.name == "llm_provider"
        assert cb.failure_threshold > 0

    def test_bulkhead_for_reads_hostility_config(self):
        bh = bulkhead_for("llm_provider")
        assert bh.name == "llm_provider"
        assert bh.max_concurrent > 0

    def test_breaker_for_is_a_singleton_per_name(self):
        assert breaker_for("llm_provider") is breaker_for("llm_provider")

    def test_unknown_boundary_raises(self):
        with pytest.raises(RuntimeError, match="Unknown hostility boundary"):
            breaker_for("does_not_exist")
