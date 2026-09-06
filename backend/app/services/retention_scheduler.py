# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Background scheduler for the retention sweep (services/retention.py).

The platform has no task-queue/cron infrastructure today (see
docs/OPERATIONAL_RUNBOOKS.md §5 — background jobs run as in-process
FastAPI BackgroundTasks, not a durable queue), so this is a daemon thread
started from `main.py`'s startup hook, matching the existing pattern used for
`_sweep_interrupted_agent_jobs()` (database.py) — best-effort, never blocks
app startup, and a failure in one sweep is logged but doesn't kill the loop.

Uses `threading.Event.wait(timeout)` rather than `time.sleep()` so `stop()`
returns promptly instead of waiting out the full interval — important for
test suites and graceful shutdown.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop_event = threading.Event()
_lock = threading.Lock()


def _loop(interval_s: float) -> None:
    from app.database import SessionLocal
    from app.services import retention

    # Import the MODULE (not `run_all` directly) and call it as
    # `retention.run_all` on each iteration — this makes monkeypatching
    # `app.services.retention.run_all` (as tests do) take effect on every
    # sweep, rather than binding a stale local reference at loop start.
    while not _stop_event.wait(interval_s):
        db = SessionLocal()
        try:
            summary = retention.run_all(db)
            if any(summary.values()):
                logger.info("retention sweep completed: %s", summary)
        except Exception:  # noqa: BLE001 — one bad sweep must not kill the loop
            logger.exception("retention sweep failed")
        finally:
            db.close()


def start(interval_s: float | None = None) -> None:
    """Start the background sweep thread. Idempotent — calling start() again
    while already running is a no-op (call stop() first to restart with a
    different interval, e.g. in tests)."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        if interval_s is None:
            from app.config import settings
            interval_s = settings.retention_sweep_interval_s
        if interval_s <= 0:
            logger.info("retention scheduler disabled (retention_sweep_interval_s <= 0)")
            return
        _stop_event.clear()
        _thread = threading.Thread(target=_loop, args=(interval_s,), daemon=True, name="retention-sweep")
        _thread.start()
        logger.info("retention scheduler started (interval=%ss)", interval_s)


def stop(timeout: float = 5.0) -> None:
    """Signal the loop to stop and join the thread. Safe to call even if
    start() was never called or the scheduler is disabled."""
    global _thread
    with _lock:
        _stop_event.set()
        t = _thread
        _thread = None
    if t is not None:
        t.join(timeout=timeout)


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()
