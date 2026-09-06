# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: agent jobs — the 202 + background-task + poll pattern for
the long-running agent endpoints (design/code/testing analyse, Open MR).

The POST endpoints validate preconditions synchronously, insert an `agent_jobs`
row, schedule the work on FastAPI BackgroundTasks (own SessionLocal, same shape
as code_repo._index_job), and return 202 immediately. The UI polls
GET /changes/{id}/jobs/{kind}/latest to drive its running/progress/error state;
the produced artifact still lands in its own report table.
"""
import logging
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.core.errors import safe_exc
from app.database import SessionLocal, get_db
from app.models import AgentJob, PartnerUser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])

JOB_KINDS = {"design", "code", "testing", "mr", "codegen", "review", "fix"}

# Runner signature: (db, progress) -> None. `progress(msg)` stamps the job row;
# raise to fail the job (str(exc) becomes the user-facing error).
JobRunner = Callable[[Session, Callable[[str], None]], None]


def job_view(j: AgentJob) -> dict:
    return {
        "job_id": j.id, "change_id": j.change_id, "kind": j.kind,
        "status": j.status, "progress": j.progress, "error": j.error,
        "error_category": j.error_category, "error_code": j.error_code,
        "created_at": j.created_at.isoformat(),
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        "tokens_used": j.tokens_used,
    }


def start_job(
    db: Session, bg: BackgroundTasks, *, change_id: str, kind: str, runner: JobRunner,
) -> dict:
    """Insert the job row + schedule the runner. 409 if one is already running
    for this (change, kind) — no double-fire from impatient clicks. 429 if the
    change has already exhausted its per-change LLM token budget (Finding 4:
    security_architecture_skills.md §4.2) — checked synchronously, BEFORE the
    202 is returned, so a non-converging loop is stopped at the API boundary
    rather than discovered after another background job has already spent
    more tokens. 503 while the platform is draining for shutdown, or when the
    cross-change concurrency cap is saturated (EA_Skills.md P2/P3).

    All four gates run BEFORE the row is inserted: a rejected dispatch must
    leave no `agent_jobs` row behind, or the UI would show a phantom job that
    never runs.
    """
    from app.core.runtime import ShuttingDownError, is_accepting

    # Drain gate (P3) — refuse new work the moment shutdown begins, so the
    # drain window is spent finishing existing jobs rather than racing an
    # inbound stream of new ones. 503 (retryable) rather than 500.
    if not is_accepting():
        raise HTTPException(
            status_code=503,
            detail="the platform is shutting down and is not accepting new agent jobs — retry shortly",
        )

    existing = db.execute(
        select(AgentJob).where(
            AgentJob.change_id == change_id,
            AgentJob.kind == kind,
            AgentJob.status == "running",
        )
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail=f"a {kind} job is already running for this change")

    from app.core.llm_budget import TokenBudgetExceeded, enforce_budget
    try:
        enforce_budget(db, change_id)
    except TokenBudgetExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc))

    job = AgentJob(change_id=change_id, kind=kind, progress="starting")
    db.add(job)
    db.commit()
    db.refresh(job)
    bg.add_task(_run_job, job.id, runner)
    return job_view(job)


def _run_job(job_id: str, runner: JobRunner) -> None:
    """Background execution — owns its session (the request's is closed by the
    time this runs).

    Wraps the runner in TWO context managers:
      - `core.llm.track_token_usage()` — sums every call_llm() invocation the
        runner makes (directly, or transitively through helpers like
        agents/code_files.py's batched generation) onto the job row (Finding 4).
      - `core.correlation.use_correlation_id(job.correlation_id)` — makes this
        job's correlation id the default for any `npci_client.send_task()`
        call the runner triggers, without threading it through every runner's
        signature (Finding 13: security_architecture_skills.md §13.1).

    And enforces two process-level guarantees (EA_Skills.md P2/P3):
      - the `agent_job_dispatch` bulkhead caps how many jobs run concurrently
        across DIFFERENT changes. This boundary was declared in
        `core/hostility.py` from the start but never actually enforced, so the
        cap it advertised did not exist;
      - the in-flight registry lets shutdown drain real work instead of killing
        it and tombstoning the rows on the next boot.
    """
    from contextlib import ExitStack

    from app.config import settings
    from app.core.correlation import use_correlation_id
    from app.core.llm import track_token_usage
    from app.core.resilience import bulkhead_for
    from app.core.runtime import register_job, unregister_job

    # `Bulkhead.acquire()` is a context manager (it releases on exit), so hold
    # it in an ExitStack rather than an acquire/release pair — that keeps the
    # release automatic on every exit path, including the ones below that
    # return early, and avoids a leaked permit permanently shrinking the cap.
    #
    # Acquired BEFORE the DB session is opened so a queued job doesn't hold a
    # pool connection while it waits — that would trade a worker bottleneck for
    # a connection-pool bottleneck, and Finding 6's pool sizing assumes
    # connections are held only while actively in use.
    with ExitStack() as stack:
        try:
            stack.enter_context(
                bulkhead_for("agent_job_dispatch").acquire(
                    timeout=settings.agent_job_bulkhead_timeout_s
                )
            )
        except Exception as exc:  # noqa: BLE001 — saturated, or boundary misconfigured
            # Type only at WARNING (CWE-209); full detail at DEBUG, which is on
            # in dev and opt-in in prod. Same split at every catch site below.
            logger.warning(
                "agent job %s rejected at the dispatch bulkhead: %s", job_id, safe_exc(exc),
            )
            logger.debug("bulkhead rejection detail for job %s", job_id, exc_info=True)
            _mark_job_rejected(
                job_id, "the platform is at its concurrent-job limit — try again in a moment",
            )
            return

        try:
            register_job(job_id)
        except Exception as exc:  # noqa: BLE001 — ShuttingDownError: drain began before execution
            logger.warning("agent job %s not started: %s", job_id, safe_exc(exc))
            logger.debug("job registration failure detail for %s", job_id, exc_info=True)
            _mark_job_rejected(
                job_id, "the platform shut down before this job started — run it again",
            )
            return
        stack.callback(unregister_job, job_id)

        _execute_job(job_id, runner, use_correlation_id, track_token_usage)


def _execute_job(job_id: str, runner: JobRunner, use_correlation_id, track_token_usage) -> None:
    """The actual job body, split out of `_run_job` so the admission control
    above (bulkhead + drain registry) reads as a short, obvious preamble rather
    than burying the work in nested `with` blocks."""
    db = SessionLocal()
    try:
        job = db.get(AgentJob, job_id)
        if job is None:  # row vanished (manual cleanup) — nothing to report to
            return
        correlation_id = job.correlation_id

        def progress(msg: str) -> None:
            job.progress = (msg or "")[:200]
            db.commit()

        try:
            with use_correlation_id(correlation_id), track_token_usage() as usage:
                runner(db, progress)
            job = db.get(AgentJob, job_id)
            job.status = "done"
            job.progress = None
            job.tokens_used = usage.total()
        except Exception as exc:  # noqa: BLE001 — classified below, then surfaced on the job row
            from app.core.errors import classify, user_facing_error
            category, code = classify(exc)
            # `logger.exception` keeps the FULL message and traceback in the
            # log — nothing is lost for debugging. What changes is what leaves
            # the process: `job.error` is returned to the browser by
            # `job_view()`, so a raw `str(exc)` from SQLAlchemy/httpx/redis
            # would hand internal hosts, paths and statements to any dashboard
            # caller (CWE-209).
            logger.exception("agent job %s failed [category=%s code=%s]", job_id, category, code)
            db.rollback()
            job = db.get(AgentJob, job_id)
            job.status = "error"
            job.error = user_facing_error(exc)
            job.error_category = category
            job.error_code = code
            job.progress = None
            job.tokens_used = usage.total()  # tokens spent before the failure still count
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def _mark_job_rejected(job_id: str, message: str) -> None:
    """Close out a job row that was never actually run (bulkhead saturated, or
    shutdown began between the 202 and execution).

    Uses its own short-lived session because the caller has not opened one yet.
    Best-effort: failing to write this row must not raise out of a background
    task, where there is no client left to receive the error."""
    db = SessionLocal()
    try:
        job = db.get(AgentJob, job_id)
        if job is None:
            return
        job.status = "error"
        job.error = message
        job.error_category = "capacity"
        job.error_code = "job_not_admitted"
        job.progress = None
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("could not mark agent job %s as rejected", job_id)
    finally:
        db.close()


@router.get("/changes/{change_id}/jobs/{kind}/latest")
def get_latest_job(
    change_id: str,
    kind: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Latest job for (change, kind) — drives the panel's button state. 404 → never run."""
    if kind not in JOB_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown job kind: {kind}")
    job = db.execute(
        select(AgentJob)
        .where(AgentJob.change_id == change_id, AgentJob.kind == kind)
        .order_by(AgentJob.created_at.desc())
        .limit(1)
    ).scalars().first()
    if job is None:
        raise HTTPException(status_code=404, detail="no job yet")
    return job_view(job)
