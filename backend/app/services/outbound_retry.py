# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Outbound A2A retry sweep — drains `OutboundA2ARetry` rows queued by
`npci_client.send_task()` on a transient failure or open circuit breaker.

Finding 12 (security_architecture_skills.md §5.4/§11.3, EA_Skills.md P7 "DLQ
and replay process"). See docs/ARCHITECTURE_REVIEW_ACTIONS.md and
docs/OPERATIONAL_RUNBOOKS.md §3.6.

Exponential backoff: 1 -> 5 -> 15 -> 60 minutes between attempts (mirrors the
NPCI-side outbound delivery backoff already documented in the codebase's own
`a2a_client.py`/Finding 12 discussion), capped at `max_attempts`. A row that
exhausts its attempts moves to `status='abandoned'` and emits a structured
security/ops event so it surfaces in alerting rather than sitting silently in
the table forever.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Minutes to wait before the Nth retry attempt (1-indexed: after attempt 1
# fails, wait _BACKOFF_MINUTES[0] before attempt 2, etc.). The last value
# repeats for any attempt beyond the list's length.
_BACKOFF_MINUTES = [1, 5, 15, 60]


def _next_backoff_minutes(attempts_so_far: int) -> int:
    idx = min(attempts_so_far, len(_BACKOFF_MINUTES) - 1)
    return _BACKOFF_MINUTES[idx]


def enqueue(
    db: Session,
    *,
    change_id: str | None,
    task_type: str,
    payload: dict,
    error: str,
    correlation_id: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    """Queue a failed send_task() call for retry. Called from
    npci_client.send_task() on any transport failure (including a
    CircuitOpenError — the breaker being open IS a transient-failure signal,
    not a reason to drop the message).

    `idempotency_key` is the envelope `message_id` the ORIGINAL attempt used.
    Persisting it is what makes the retry safe (EA_Skills.md P3): every later
    attempt re-sends under the same id, so a receiver that already processed
    the first attempt recognises the duplicate instead of acting on it twice.
    Falls back to a fresh uuid4 only when the caller could not supply one (the
    send failed before an id was minted), which still beats leaving it NULL —
    the retries of THAT row remain mutually idempotent.
    """
    import uuid

    from app.models import OutboundA2ARetry

    db.add(OutboundA2ARetry(
        change_id=change_id,
        task_type=task_type,
        payload=payload,
        correlation_id=correlation_id,
        idempotency_key=idempotency_key or str(uuid.uuid4()),
        last_error=(error or "")[:500],
        next_retry_at=datetime.now(timezone.utc) + timedelta(minutes=_BACKOFF_MINUTES[0]),
    ))
    db.commit()
    logger.warning(
        "A2A send failed, queued for retry: type=%s change=%s error=%s",
        task_type, change_id, (error or "")[:200],
    )


def run_sweep(db: Session, *, max_attempts: int | None = None) -> dict:
    """Retry every due (`next_retry_at <= now`, `status='pending'`) row once.
    Returns {"delivered": n, "requeued": n, "abandoned": n}.

    Calls `_dispatch_wire()` directly rather than `send_task()` — this sweep
    IS the retry mechanism, so it must not re-enqueue a fresh
    `OutboundA2ARetry` row on failure (which `send_task()`'s own error path
    does); it updates THIS row's `attempts`/`next_retry_at` instead. It also
    deliberately bypasses the circuit breaker: `send_task()`'s breaker exists
    to protect interactive, UI-triggered calls from hanging on a known-down
    NPCI; this background sweep already paces itself via the backoff
    schedule and should make a real attempt each time a row comes due,
    independent of the interactive path's breaker state."""
    # Import here (not at module top) to avoid a circular import — npci_client
    # imports this module, and this module needs npci_client's send helper.
    from app.config import settings
    from app.models import OutboundA2ARetry
    # Shared transport helper. `_dispatch_wire` is async since ITA-3; this
    # sweep runs in the scheduler's own plain thread (no loop), so the
    # portable bridge lands on its `asyncio.run` branch — the same isolation
    # the old inline `asyncio.run` gave it, now in one audited place.
    from app.npci_client import _dispatch_wire, _run_portably

    if max_attempts is None:
        max_attempts = settings.outbound_retry_max_attempts

    now = datetime.now(timezone.utc)
    due = db.execute(
        select(OutboundA2ARetry).where(
            OutboundA2ARetry.status == "pending",
            OutboundA2ARetry.next_retry_at <= now,
        )
    ).scalars().all()

    counts = {"delivered": 0, "requeued": 0, "abandoned": 0}
    for row in due:
        # ITA-5: a queued tunnelled exchange is never replayed — it would be a
        # duplicate business call on the far side. The enqueue path excludes
        # them; this abandons any row that predates the exclusion, loudly.
        from app.a2a_common.integration_contract import TUNNEL_TASK_TYPES

        if row.task_type in TUNNEL_TASK_TYPES:
            row.status = "abandoned"
            row.last_error = "tunnel exchange — replay would duplicate a business call"
            counts["abandoned"] += 1
            continue
        row.attempts += 1
        try:
            # Re-send under the ORIGINAL envelope message_id so NPCI can
            # deduplicate an attempt it already processed (EA_Skills.md P3).
            _run_portably(_dispatch_wire(
                db, row.task_type, row.change_id, row.payload,
                job_correlation_id=row.correlation_id,
                idempotency_key=row.idempotency_key,
            ))
        except Exception as exc:  # noqa: BLE001 — CircuitOpenError or any transport error
            # Type only — matches the enqueue path in npci_client.send_task;
            # this row is rendered in the retry-queue view (CWE-209).
            from app.core.errors import safe_exc
            row.last_error = safe_exc(exc)
            logger.debug(
                "A2A retry attempt failed: type=%s change=%s", row.task_type, row.change_id,
                exc_info=True,
            )
            if row.attempts >= max_attempts:
                row.status = "abandoned"
                counts["abandoned"] += 1
                from app.core.security_events import emit_security_event
                emit_security_event(
                    event_name="outbound_a2a_delivery_abandoned",
                    severity="high",
                    boundary="npci_a2a_outbound",
                    decision="abandoned",
                    reason_code=f"attempts={row.attempts}",
                    correlation_id=row.correlation_id,
                )
                logger.error(
                    "A2A outbound retry ABANDONED after %d attempts: type=%s change=%s",
                    row.attempts, row.task_type, row.change_id,
                )
            else:
                row.next_retry_at = now + timedelta(minutes=_next_backoff_minutes(row.attempts))
                counts["requeued"] += 1
        else:
            row.status = "delivered"
            counts["delivered"] += 1
            logger.info(
                "A2A outbound retry delivered on attempt %d: type=%s change=%s",
                row.attempts, row.task_type, row.change_id,
            )
        db.commit()

    return counts
