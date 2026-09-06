# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Hostility-tier registry — the platform's single source of truth for every
interface's trust tier and per-tier operational limits.

Per security_architecture_skills.md §4: every interface MUST be classified
H1 (intra-domain), H2 (cross-domain internal), or H3 (external/partner-facing),
with externally configurable size/rate/timeout/bulkhead limits validated at
startup. Missing/unsafe configuration MUST fail fast, not start insecurely.

See docs/adr/ADR-0004-hostility-tier-registry.md for the design record and
docs/SECURITY_ARCHITECTURE.md §4 for the classification table this module
implements.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from app.config import settings

logger = logging.getLogger(__name__)


class HostilityTier(str, Enum):
    H1 = "H1"  # intra-domain (in-process agent calls, internal DB access)
    H2 = "H2"  # cross-domain internal (Ollama, pgvector, GitLab API — trusted infra, still validated)
    H3 = "H3"  # external / partner-facing (NPCI A2A ingress/egress, LLM provider egress)


@dataclass(frozen=True)
class BoundaryLimits:
    name: str
    tier: HostilityTier
    max_request_bytes: int
    timeout_connect_s: float
    timeout_read_s: float
    rate_limit_rps: int
    circuit_breaker_failure_threshold: int
    circuit_breaker_cooldown_s: float
    bulkhead_max_concurrent: int
    validation_strictness: str  # "strict" | "tolerant"


def _build_boundaries() -> dict[str, BoundaryLimits]:
    """Built as a function (not a module-level constant) so it always reflects
    the current `settings` — important for tests that construct a fresh
    Settings object with overridden values."""
    return {
        "a2a_inbound": BoundaryLimits(
            name="a2a_inbound",
            tier=HostilityTier.H3,
            max_request_bytes=settings.a2a_max_request_body_bytes,
            timeout_connect_s=5.0,
            timeout_read_s=30.0,
            rate_limit_rps=settings.a2a_rate_limit_rps,
            circuit_breaker_failure_threshold=0,  # inbound — N/A, no breaker on receive side
            circuit_breaker_cooldown_s=0.0,
            bulkhead_max_concurrent=settings.a2a_inbound_max_concurrent,
            validation_strictness="strict",
        ),
        "npci_a2a_outbound": BoundaryLimits(
            name="npci_a2a_outbound",
            tier=HostilityTier.H3,
            max_request_bytes=10 * 1024 * 1024,
            timeout_connect_s=5.0,
            timeout_read_s=settings.npci_outbound_timeout_s,
            rate_limit_rps=0,  # outbound — governed by NPCI's own ingress limits
            circuit_breaker_failure_threshold=settings.npci_cb_failure_threshold,
            circuit_breaker_cooldown_s=settings.npci_cb_cooldown_s,
            bulkhead_max_concurrent=settings.npci_outbound_max_concurrent,
            validation_strictness="strict",
        ),
        "llm_provider": BoundaryLimits(
            name="llm_provider",
            tier=HostilityTier.H3,
            max_request_bytes=0,  # bounded by max_tokens, not byte size
            timeout_connect_s=10.0,
            timeout_read_s=settings.llm_read_timeout_s,
            rate_limit_rps=0,
            circuit_breaker_failure_threshold=settings.llm_cb_failure_threshold,
            circuit_breaker_cooldown_s=settings.llm_cb_cooldown_s,
            bulkhead_max_concurrent=settings.llm_max_concurrent_calls,
            validation_strictness="strict",
        ),
        "gitlab_api": BoundaryLimits(
            name="gitlab_api",
            tier=HostilityTier.H2,
            max_request_bytes=0,
            timeout_connect_s=5.0,
            timeout_read_s=60.0,
            rate_limit_rps=0,
            circuit_breaker_failure_threshold=5,
            circuit_breaker_cooldown_s=20.0,
            bulkhead_max_concurrent=4,
            validation_strictness="tolerant",
        ),
        "ollama_embed": BoundaryLimits(
            name="ollama_embed",
            tier=HostilityTier.H2,
            max_request_bytes=0,
            timeout_connect_s=5.0,
            timeout_read_s=120.0,
            rate_limit_rps=0,
            circuit_breaker_failure_threshold=8,
            circuit_breaker_cooldown_s=15.0,
            bulkhead_max_concurrent=6,
            validation_strictness="tolerant",
        ),
        "agent_job_dispatch": BoundaryLimits(
            name="agent_job_dispatch",
            tier=HostilityTier.H1,
            max_request_bytes=0,
            timeout_connect_s=0.0,
            timeout_read_s=0.0,
            rate_limit_rps=0,
            circuit_breaker_failure_threshold=0,
            circuit_breaker_cooldown_s=0.0,
            bulkhead_max_concurrent=settings.agentic_max_concurrent_runs,
            validation_strictness="strict",
        ),
    }


# Public, importable snapshot — most call sites (rate limiting, circuit
# breaker construction, body-size checks) read this directly. Tests that need
# a fresh view after mutating settings should call `_build_boundaries()`
# themselves or reload this module.
BOUNDARIES: dict[str, BoundaryLimits] = _build_boundaries()


def validate_at_startup() -> None:
    """Fail fast if any boundary's config is missing, zero where it must be
    positive, or otherwise unsafe. Call this from `main.py`'s startup hook,
    BEFORE init_db()."""
    boundaries = _build_boundaries()
    errors: list[str] = []
    for name, b in boundaries.items():
        if b.tier == HostilityTier.H3 and b.bulkhead_max_concurrent <= 0:
            errors.append(f"{name}: H3 boundary must have bulkhead_max_concurrent > 0")
        if name in ("npci_a2a_outbound", "llm_provider") and b.timeout_read_s <= 0:
            errors.append(f"{name}: H3 boundary must have a positive read timeout")
        if name == "a2a_inbound" and b.max_request_bytes <= 0:
            errors.append("a2a_inbound: max_request_bytes must be positive")
        if name == "a2a_inbound" and b.rate_limit_rps <= 0:
            errors.append("a2a_inbound: rate_limit_rps must be positive")
    if errors:
        raise RuntimeError(
            "Hostility-tier configuration validation failed:\n  - " + "\n  - ".join(errors)
        )
    global BOUNDARIES
    BOUNDARIES = boundaries
    logger.info(
        "Hostility-tier configuration validated: %d boundaries (%s)",
        len(BOUNDARIES),
        ", ".join(f"{n}={b.tier.value}" for n, b in BOUNDARIES.items()),
    )


def get(name: str) -> BoundaryLimits:
    try:
        return BOUNDARIES[name]
    except KeyError:
        raise RuntimeError(f"Unknown hostility boundary: {name!r} — register it in core/hostility.py")
