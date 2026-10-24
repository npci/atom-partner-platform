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
        "created_at": j.created_at.isoformat(),
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
    }


def start_job(
    db: Session, bg: BackgroundTasks, *, change_id: str, kind: str, runner: JobRunner,
) -> dict:
    """Insert the job row + schedule the runner. 409 if one is already running
    for this (change, kind) — no double-fire from impatient clicks."""
    existing = db.execute(
        select(AgentJob).where(
            AgentJob.change_id == change_id,
            AgentJob.kind == kind,
            AgentJob.status == "running",
        )
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail=f"a {kind} job is already running for this change")
    job = AgentJob(change_id=change_id, kind=kind, progress="starting")
    db.add(job)
    db.commit()
    db.refresh(job)
    bg.add_task(_run_job, job.id, runner)
    return job_view(job)


def _run_job(job_id: str, runner: JobRunner) -> None:
    """Background execution — owns its session (the request's is closed by the
    time this runs)."""
    db = SessionLocal()
    try:
        job = db.get(AgentJob, job_id)
        if job is None:  # row vanished (manual cleanup) — nothing to report to
            return

        def progress(msg: str) -> None:
            job.progress = (msg or "")[:200]
            db.commit()

        try:
            runner(db, progress)
            job = db.get(AgentJob, job_id)
            job.status = "done"
            job.progress = None
        except Exception as exc:  # noqa: BLE001 — surfaced on the job row
            logger.exception("agent job %s failed", job_id)
            db.rollback()
            job = db.get(AgentJob, job_id)
            job.status = "error"
            job.error = str(exc)[:500]
            job.progress = None
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
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
