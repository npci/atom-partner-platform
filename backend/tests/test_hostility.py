# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the hostility-tier registry (docs/adr/ADR-0004-hostility-tier-registry.md).

Covers: startup validation fails fast on unsafe config, boundaries expose
the expected tier/limits, unknown boundary name raises.
"""
import pytest

from app.config import settings
from app.core import hostility


def test_default_boundaries_validate_successfully():
    hostility.validate_at_startup()  # must not raise with shipped defaults


def test_a2a_inbound_is_h3_with_positive_limits():
    b = hostility.get("a2a_inbound")
    assert b.tier == hostility.HostilityTier.H3
    assert b.max_request_bytes > 0
    assert b.rate_limit_rps > 0
    assert b.bulkhead_max_concurrent > 0


def test_llm_provider_is_h3_with_positive_timeout_and_breaker():
    b = hostility.get("llm_provider")
    assert b.tier == hostility.HostilityTier.H3
    assert b.timeout_read_s > 0
    assert b.circuit_breaker_failure_threshold > 0
    assert b.bulkhead_max_concurrent > 0


def test_unknown_boundary_raises():
    with pytest.raises(RuntimeError, match="Unknown hostility boundary"):
        hostility.get("does_not_exist")


def test_validation_fails_fast_on_zero_body_limit(monkeypatch):
    monkeypatch.setattr(settings, "a2a_max_request_body_bytes", 0)
    with pytest.raises(RuntimeError, match="max_request_bytes must be positive"):
        hostility.validate_at_startup()


def test_validation_fails_fast_on_zero_rate_limit(monkeypatch):
    monkeypatch.setattr(settings, "a2a_rate_limit_rps", 0)
    with pytest.raises(RuntimeError, match="rate_limit_rps must be positive"):
        hostility.validate_at_startup()


def test_validation_fails_fast_on_zero_bulkhead(monkeypatch):
    monkeypatch.setattr(settings, "llm_max_concurrent_calls", 0)
    with pytest.raises(RuntimeError, match="bulkhead_max_concurrent > 0"):
        hostility.validate_at_startup()


def test_validation_fails_fast_on_zero_llm_timeout(monkeypatch):
    monkeypatch.setattr(settings, "llm_read_timeout_s", 0)
    with pytest.raises(RuntimeError, match="positive read timeout"):
        hostility.validate_at_startup()
