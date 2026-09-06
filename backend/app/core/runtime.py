# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Process-level runtime guarantees: the single-instance assumption, and
graceful drain of in-flight agent jobs on shutdown.

Closes two EA_Skills.md gaps that the original 16-finding review did not cover
because it scoped to resilience/secrets/the A2A boundary rather than walking
all ten principles:

  * **P2 — Mechanical Sympathy and Shared-Nothing Concurrency.** "Flag when:
    shared mutable state is accessed by multiple threads." The A2A rate
    limiter (`a2a_common/rate_limit_middleware.py`) and the revision-context
    cache (`agents/revision_context.py`) are per-PROCESS structures. Under a
    multi-worker/multi-replica deployment each process would enforce its own
    independent rate-limit window, so the effective limit becomes
    `configured_limit x worker_count` — a silent security regression, since
    the operator still reads "100 rps" in the config. Rather than let that
    happen quietly, `validate_single_instance()` refuses to boot.

  * **P3 — Autoscaling as a First-Class Architectural Capability.** "Flag
    when: scale-in can terminate in-flight work." Agent jobs run on FastAPI
    BackgroundTasks; a container stop killed them mid-flight, and the only
    remedy was `database._sweep_interrupted_agent_jobs()` marking the wreckage
    `error` on the NEXT boot. That is a tombstone, not a drain. The registry
    below tracks in-flight jobs so shutdown can stop admitting new work, wait
    for existing work within a bounded window, and mark only genuine
    stragglers.

Deliberate scope boundary: this makes single-instance operation *enforced and
explicit* rather than assumed. It does not make the platform horizontally
scalable — that needs shared state (Redis-backed limiter, a durable queue),
which is an infrastructure decision for the partner forking this reference
platform, not one to impose here. See `docs/SECURITY_ARCHITECTURE.md` §5.
"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


# ── P2: single-instance enforcement ──────────────────────────────────────────

# Env vars set by the common ASGI servers/orchestrators to request more than one
# worker process. Checked by name because the app cannot otherwise observe its
# own sibling workers: each is a separate process with its own memory, so a
# per-process structure has no way to detect that peers exist.
_WORKER_COUNT_ENV_VARS = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS")


def detect_worker_count() -> tuple[int, str | None]:
    """Return (worker_count, source_env_var). (1, None) when nothing requests
    multiple workers. Unparseable values are treated as 1 rather than raising —
    a malformed env var must not be the thing that takes the service down."""
    for var in _WORKER_COUNT_ENV_VARS:
        raw = os.getenv(var, "").strip()
        if not raw:
            continue
        try:
            count = int(raw)
        except ValueError:
            logger.warning("%s=%r is not an integer — ignoring for the worker-count check", var, raw)
            continue
        if count > 1:
            return count, var
    return 1, None


def validate_single_instance() -> None:
    """Refuse to boot multi-worker unless the operator has explicitly accepted
    the consequences via `PARTNER_ALLOW_MULTI_WORKER=true`.

    Fail-closed, matching the platform's other startup validators
    (`core/hostility.validate_at_startup`, config.py's TLS/DATABASE_URL
    checks): a misconfiguration that silently weakens a security control is
    worse than a refused start, because the weakened control still *looks*
    configured.
    """
    from app.config import settings

    count, source = detect_worker_count()
    if count <= 1:
        return

    # A shared rate-limit backend removes the reason this boot is refused:
    # the objection is that N workers enforce N independent windows, and a
    # redis-backed window is one window no matter how many workers hold it.
    # This is the supported way to scale horizontally — no acknowledgement
    # flag required, because nothing is being weakened.
    from app.a2a_common.rate_limit_middleware import shared_limiter_configured

    if shared_limiter_configured():
        logger.info(
            "%s=%d with a shared (redis-backed) A2A rate limiter — the ingress "
            "limit is enforced once across all workers. Note the revision-context "
            "cache is still per-process; it is a cache, so the cost is duplicated "
            "work rather than a weakened control.",
            source, count,
        )
        return

    if settings.partner_allow_multi_worker:
        logger.warning(
            "%s=%d with PARTNER_ALLOW_MULTI_WORKER=true — the A2A rate limit is "
            "enforced PER PROCESS, so the effective limit is now ~%dx the configured "
            "value, and the revision-context cache is split across workers. Only safe "
            "with a shared (Redis-backed) limiter.",
            source, count, count,
        )
        return

    raise RuntimeError(
        f"{source}={count} requests multiple worker processes, but A2A rate limiting "
        f"and the revision-context cache are per-process, in-memory structures. Each "
        f"worker would enforce its own independent rate-limit window, making the real "
        f"limit roughly {count}x the configured value while the config still reads the "
        f"single-process number.\n"
        f"  * Intended single-instance (the documented deployment): unset {source} or "
        f"set it to 1.\n"
        f"  * Genuinely need multiple workers: set PARTNER_RATE_LIMIT_REDIS_URL to "
        f"a reachable redis and install the optional `redis` package. The ingress "
        f"limit then becomes shared and this check passes on its own — no "
        f"acknowledgement flag needed, because nothing is weakened.\n"
        f"  * Accept a per-process limit anyway: set PARTNER_ALLOW_MULTI_WORKER=true "
        f"to acknowledge that the real limit becomes roughly {count}x the "
        f"configured value.\n"
        f"See docs/SECURITY_ARCHITECTURE.md §5 (High Availability Design)."
    )


# ── P3: in-flight job registry + graceful drain ──────────────────────────────

_inflight: set[str] = set()
_inflight_lock = threading.Lock()
_accepting = threading.Event()
_accepting.set()  # accept work by default; cleared on shutdown


class ShuttingDownError(RuntimeError):
    """Raised when a job is dispatched during shutdown. Surfaced to the caller
    as HTTP 503 — a retryable signal, distinct from a 500, so a client (or an
    orchestrator rolling the deployment) knows to retry against the replacement
    instance rather than treating the change as failed."""


def is_accepting() -> bool:
    return _accepting.is_set()


def register_job(job_id: str) -> None:
    """Mark a job in-flight. Raises ShuttingDownError if the drain has begun —
    checked and inserted under one lock so a job cannot slip in between the
    check and the insert and be missed by the drain."""
    with _inflight_lock:
        if not _accepting.is_set():
            raise ShuttingDownError(
                "the platform is shutting down and is not accepting new agent jobs"
            )
        _inflight.add(job_id)


def unregister_job(job_id: str) -> None:
    """Mark a job finished. Safe to call twice (discard, not remove) — the
    caller runs it from a `finally`, which can execute on paths where
    registration never happened."""
    with _inflight_lock:
        _inflight.discard(job_id)


def inflight_count() -> int:
    with _inflight_lock:
        return len(_inflight)


def inflight_job_ids() -> list[str]:
    with _inflight_lock:
        return sorted(_inflight)


def stop_accepting() -> None:
    _accepting.clear()


def resume_accepting_for_tests() -> None:
    """Reset to the accepting state and clear the registry. Tests only — a
    shutdown is one-way in production."""
    with _inflight_lock:
        _inflight.clear()
    _accepting.set()


def drain(timeout_s: float, poll_interval_s: float = 0.1) -> tuple[int, float]:
    """Stop admitting new jobs, then wait up to `timeout_s` for in-flight ones
    to finish.

    Returns (remaining_job_count, elapsed_seconds). A non-zero remaining count
    means the window expired with work still running; the caller marks those
    rows so the UI shows a clear error rather than a forever-spinner.

    Polls rather than using a condition variable: the job count changes at most
    a handful of times during a drain, the loop runs only on shutdown, and
    polling keeps `unregister_job` free of notification bookkeeping on the hot
    path. This is not a `thread.sleep` coordination anti-pattern (EA_Skills.md
    P2) — it is a bounded terminal wait with a deadline, not inter-thread
    signalling.
    """
    stop_accepting()
    started = time.monotonic()

    if timeout_s <= 0:
        remaining = inflight_count()
        if remaining:
            logger.warning("drain disabled (timeout=%s) — %d job(s) cut short", timeout_s, remaining)
        return remaining, 0.0

    deadline = started + timeout_s
    while True:
        remaining = inflight_count()
        if remaining == 0:
            elapsed = time.monotonic() - started
            logger.info("drain complete: all agent jobs finished in %.1fs", elapsed)
            return 0, elapsed
        if time.monotonic() >= deadline:
            elapsed = time.monotonic() - started
            logger.warning(
                "drain timed out after %.1fs with %d job(s) still running: %s",
                elapsed, remaining, inflight_job_ids(),
            )
            return remaining, elapsed
        time.sleep(poll_interval_s)
