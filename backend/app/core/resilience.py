# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Circuit breaker + bulkhead primitives for outbound dependency calls.

Deliberately minimal (no external library dependency) — a per-process,
in-memory circuit breaker is sufficient for the platform's documented
single-instance deployment (docker-compose.yml runs exactly one `backend`
container); if the platform is later run with multiple worker processes,
back this with a shared store (Redis) instead of in-memory state — see
docs/SECURITY_ARCHITECTURE.md §5 (High Availability Design).

security_architecture_skills.md §5.4 (Adapter Layer) + §11.3 (Mandatory
Resilience Patterns): every dependency call MUST have a timeout, circuit
breaker, and bulkhead. See docs/adr/ADR-0001-llm-circuit-breaker-and-bulkhead.md.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class CircuitOpenError(RuntimeError):
    """Raised instead of calling a dependency whose circuit is open."""


class CircuitBreaker:
    """Simple three-state breaker: closed -> open (after N consecutive
    failures) -> half-open (after cooldown, allows ONE trial call) -> closed
    (on trial success) or open again (on trial failure)."""

    def __init__(self, name: str, *, failure_threshold: int, cooldown_s: float):
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_s = cooldown_s
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._state = "closed"
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if self._state == "open" and (time.monotonic() - self._opened_at) >= self.cooldown_s:
            self._state = "half_open"
            logger.info("circuit_breaker[%s]: open -> half_open (cooldown elapsed)", self.name)

    @contextmanager
    def call(self):
        with self._lock:
            self._maybe_half_open()
            if self._state == "open":
                raise CircuitOpenError(
                    f"circuit '{self.name}' is open — dependency considered unhealthy, "
                    f"failing fast instead of calling it"
                )
        try:
            yield
        except Exception:
            with self._lock:
                self._consecutive_failures += 1
                if self._state == "half_open" or self._consecutive_failures >= self.failure_threshold:
                    if self._state != "open":
                        logger.warning(
                            "circuit_breaker[%s]: -> open (%d consecutive failures, threshold=%d)",
                            self.name, self._consecutive_failures, self.failure_threshold,
                        )
                        from app.core.security_events import emit_security_event
                        emit_security_event(
                            event_name="circuit_breaker_opened",
                            severity="high",
                            boundary=self.name,
                            decision="isolated",
                            reason_code=f"consecutive_failures={self._consecutive_failures}",
                        )
                    self._state = "open"
                    self._opened_at = time.monotonic()
            raise
        else:
            with self._lock:
                if self._consecutive_failures or self._state != "closed":
                    logger.info("circuit_breaker[%s]: -> closed (call succeeded)", self.name)
                self._consecutive_failures = 0
                self._state = "closed"


class Bulkhead:
    """Bounded concurrency gate — a thin wrapper over threading.BoundedSemaphore
    with a non-blocking-with-timeout acquire so callers get an immediate,
    clear rejection instead of silently queueing forever."""

    def __init__(self, name: str, *, max_concurrent: int):
        self.name = name
        self.max_concurrent = max(1, max_concurrent)
        self._sem = threading.BoundedSemaphore(self.max_concurrent)

    @contextmanager
    def acquire(self, *, timeout: float | None = 30.0):
        acquired = self._sem.acquire(blocking=True, timeout=timeout)
        if not acquired:
            raise RuntimeError(
                f"bulkhead '{self.name}' saturated (max_concurrent={self.max_concurrent}) — "
                f"rejecting instead of queueing unbounded"
            )
        try:
            yield
        finally:
            self._sem.release()


# ── Process-wide singletons, one per hostility.BOUNDARIES entry that needs
# resilience wrapping. Constructed lazily from the hostility registry so the
# limits stay centrally configured (docs/adr/ADR-0004-hostility-tier-registry.md). ──
_breakers: dict[str, CircuitBreaker] = {}
_bulkheads: dict[str, Bulkhead] = {}
_registry_lock = threading.Lock()


def breaker_for(boundary_name: str) -> CircuitBreaker:
    with _registry_lock:
        if boundary_name not in _breakers:
            from app.core.hostility import get as get_boundary
            b = get_boundary(boundary_name)
            _breakers[boundary_name] = CircuitBreaker(
                boundary_name,
                failure_threshold=b.circuit_breaker_failure_threshold or 5,
                cooldown_s=b.circuit_breaker_cooldown_s or 30.0,
            )
        return _breakers[boundary_name]


def bulkhead_for(boundary_name: str) -> Bulkhead:
    with _registry_lock:
        if boundary_name not in _bulkheads:
            from app.core.hostility import get as get_boundary
            b = get_boundary(boundary_name)
            _bulkheads[boundary_name] = Bulkhead(
                boundary_name, max_concurrent=b.bulkhead_max_concurrent or 8,
            )
        return _bulkheads[boundary_name]


def reset_for_tests() -> None:
    """Test hook — clear cached breaker/bulkhead singletons so a test that
    mutates hostility limits gets fresh instances."""
    with _registry_lock:
        _breakers.clear()
        _bulkheads.clear()
