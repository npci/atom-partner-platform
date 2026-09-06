# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Data retention / purge policy.

security_architecture_skills.md §10.3 (retention + purgeability are mandatory
dataset properties), EA_Skills.md P6 (TTL, validity, archival, and temporality
rules). See docs/ARCHITECTURE_REVIEW_ACTIONS.md Finding 7.

Two independent sweeps:
  - purge_superseded_generated_code_files: keeps only the most recent N
    generated-file iterations per change (older ones are fully superseded by
    a later "Apply Fixes" round and have no further review/audit value once
    the change has moved on).
  - purge_stale_agent_run_payloads: nulls out AgentRun.result_payload past a
    retention window, keeping the audit ROW (status/latency/timestamps)
    permanently — only the potentially-large JSON body is cleared.

Neither sweep touches `change_documents` — those are the partner's working
copy of NPCI-issued content and are NOT purged by this module (see
docs/SECURITY_ARCHITECTURE.md §10's data classification table for the
rationale: they're a justified, non-owner working copy, not a cache).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def purge_superseded_generated_code_files(db: Session, *, keep_latest_n: int | None = None) -> int:
    """For each change_id, keep only the most recent `keep_latest_n`
    GeneratedCodeFile iterations; delete older ones. The CURRENT iteration and
    its review reports are never touched — only superseded-and-far-enough-back
    iterations are purged. Returns the number of rows deleted."""
    from app.config import settings
    from app.models import GeneratedCodeFile

    if keep_latest_n is None:
        keep_latest_n = settings.retention_keep_latest_iterations
    if keep_latest_n <= 0:
        return 0  # explicit opt-out — never purge

    change_ids = [
        r[0] for r in db.execute(select(GeneratedCodeFile.change_id).distinct()).all()
    ]
    total_deleted = 0
    for change_id in change_ids:
        iterations = sorted(
            i for (i,) in db.execute(
                select(GeneratedCodeFile.iteration)
                .where(GeneratedCodeFile.change_id == change_id)
                .distinct()
            ).all()
        )
        to_delete = iterations[:-keep_latest_n] if len(iterations) > keep_latest_n else []
        if not to_delete:
            continue
        result = db.execute(
            delete(GeneratedCodeFile).where(
                GeneratedCodeFile.change_id == change_id,
                GeneratedCodeFile.iteration.in_(to_delete),
            )
        )
        total_deleted += result.rowcount or 0
    db.commit()
    if total_deleted:
        logger.info("retention: purged %d superseded GeneratedCodeFile row(s)", total_deleted)
    return total_deleted


def purge_stale_agent_run_payloads(db: Session, *, older_than_days: int | None = None) -> int:
    """Null out AgentRun.result_payload for runs completed before the
    retention window; the row (status, latency, timestamps, error_message)
    stays permanently. Returns the number of rows cleared."""
    from app.config import settings
    from app.models import AgentRun

    if older_than_days is None:
        older_than_days = settings.retention_agent_run_payload_days
    if older_than_days <= 0:
        return 0  # explicit opt-out — never purge

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    result = db.execute(
        AgentRun.__table__.update()
        .where(AgentRun.completed_at < cutoff, AgentRun.result_payload.isnot(None))
        .values(result_payload=None)
    )
    db.commit()
    if result.rowcount:
        logger.info("retention: cleared %d stale AgentRun payload(s)", result.rowcount)
    return result.rowcount or 0


def run_all(db: Session) -> dict:
    """Run every retention sweep once. Returns a summary dict for logging /
    the operator-facing admin endpoint (see api/dashboard — a manual trigger
    is useful for §3.7 of OPERATIONAL_RUNBOOKS.md's disk-capacity runbook)."""
    return {
        "generated_code_files_purged": purge_superseded_generated_code_files(db),
        "agent_run_payloads_cleared": purge_stale_agent_run_payloads(db),
    }
