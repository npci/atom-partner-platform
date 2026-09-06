# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Background scheduler for the outbound A2A retry sweep
(services/outbound_retry.py). Same daemon-thread pattern as
services/retention_scheduler.py, but on a much shorter interval — retries
need to be attempted every minute or so, not once a day, so that the
1/5/15/60-minute backoff schedule in outbound_retry.py actually has a chance
to run each due row promptly.

See docs/ARCHITECTURE_REVIEW_ACTIONS.md Finding 12 and
docs/OPERATIONAL_RUNBOOKS.md §3.6.
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
    from app.services import outbound_retry

    while not _stop_event.wait(interval_s):
        db = SessionLocal()
        try:
            counts = outbound_retry.run_sweep(db)
            if any(counts.values()):
                logger.info("outbound A2A retry sweep completed: %s", counts)
        except Exception:  # noqa: BLE001 — one bad sweep must not kill the loop
            logger.exception("outbound A2A retry sweep failed")
        finally:
            db.close()


def start(interval_s: float | None = None) -> None:
    """Start the background sweep thread. Idempotent."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        if interval_s is None:
            from app.config import settings
            interval_s = settings.outbound_retry_sweep_interval_s
        if interval_s <= 0:
            logger.info("outbound retry scheduler disabled (outbound_retry_sweep_interval_s <= 0)")
            return
        _stop_event.clear()
        _thread = threading.Thread(
            target=_loop, args=(interval_s,), daemon=True, name="outbound-a2a-retry-sweep",
        )
        _thread.start()
        logger.info("outbound A2A retry scheduler started (interval=%ss)", interval_s)


def stop(timeout: float = 5.0) -> None:
    global _thread
    with _lock:
        _stop_event.set()
        t = _thread
        _thread = None
    if t is not None:
        t.join(timeout=timeout)


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()
