# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: testing — run the partner Test agent against a change to
author a test plan (partner-side cases + coverage mapping to NPCI cert cases),
persist a versioned plan, serve it (JSON + .docx download).

The agent AUTHORS only. Execution of the authoritative certification suite is
delegated to NPCI via the existing cert-readiness lifecycle (dashboard
/certification.py) — this module does not run tests.

Module is `testing.py` (not `test.py`) to avoid pytest discovery; the registry
agent name is "test".
"""
import json
import logging
from urllib.parse import quote as urlquote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import registry
from app.api.auth import get_current_user
from app.database import get_db
from app.models import DesignReport, IncomingChange, PartnerSetting, PartnerUser, TestReport
from app.rag.retrieval import build_kb_context

from ._shared import markdown_to_docx_bytes

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


def _kb_context(db: Session, change: IncomingChange) -> str:
    query = f"{change.title or ''} {(change.enhanced_prompt or change.initial_prompt or '')[:500]}".strip()
    return build_kb_context(db, query)


def _runtime_api_key(db: Session) -> str | None:
    row = db.get(PartnerSetting, "partner_anthropic_api_key")
    return row.value if row and row.value else None


def _latest_design_summary(db: Session, change_id: str) -> str | None:
    """The newest design one-liner, so the test plan builds on the design."""
    row = db.execute(
        select(DesignReport)
        .where(DesignReport.change_id == change_id)
        .order_by(DesignReport.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not row:
        return None
    try:
        return (json.loads(row.content) or {}).get("one_line_summary")
    except (json.JSONDecodeError, TypeError):
        return None


def _run_test_job(change_id: str, db: Session, progress) -> None:
    """The actual test-plan run — executes inside a background AgentJob."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise RuntimeError(f"unknown change_id: {change_id}")

    api_key = _runtime_api_key(db)

    progress("assembling change context")
    from app.agents.revision_context import assemble_change_context
    ctx = assemble_change_context(db, change_id, api_key)

    agent_input = {
        "change_title": change.title,
        "change_initial_prompt": change.initial_prompt,
        "change_enhanced_prompt": change.enhanced_prompt,
        "documents": ctx["documents"],
        "revision_summary": ctx["revision_summary"],
        "design_summary": _latest_design_summary(db, change_id),
        "knowledge_context": _kb_context(db, change),
        "api_key": api_key,
        "change_id": change_id,
    }
    progress("generating test plan (takes a few minutes)")
    try:
        report = registry.get("test").execute(agent_input, db=db, change_id=change_id)
    except Exception as exc:  # noqa: BLE001 — surfaced on the job row + audited in agent_runs
        # The agent raises with the specific cause; don't bury it under a list
        # of guesses. (A missing LLM key is NOT a cause — that path short-
        # circuits to a mock plan in TestAgent.run and never reaches here.)
        raise RuntimeError(f"test agent failed — {exc}") from exc

    prior = db.execute(
        select(TestReport.version).where(TestReport.change_id == change_id)
    ).scalars().all()
    next_version = (max(prior) + 1) if prior else 1

    meta = report.get("_meta", {})
    row = TestReport(
        change_id=change_id,
        version=next_version,
        content=json.dumps(report),
        profile_version=meta.get("profile_version"),
        model_used=meta.get("model_used"),
    )
    db.add(row)
    db.commit()


@router.post("/changes/{change_id}/test/analyse", status_code=202)
def run_test_plan(
    change_id: str,
    bg: BackgroundTasks,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Kick a background Test-agent run; returns 202 + a job id immediately.
    Poll GET /changes/{id}/jobs/testing/latest; the plan lands in test_reports."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise HTTPException(status_code=404, detail=f"unknown change_id: {change_id}")
    from .jobs import start_job
    return start_job(
        db, bg, change_id=change_id, kind="testing",
        runner=lambda jdb, progress: _run_test_job(change_id, jdb, progress),
    )


def _latest_test_row(db: Session, change_id: str) -> TestReport | None:
    return db.execute(
        select(TestReport)
        .where(TestReport.change_id == change_id)
        .order_by(TestReport.version.desc())
        .limit(1)
    ).scalar_one_or_none()


@router.get("/changes/{change_id}/test/report")
def get_test_report(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the highest-version persisted test plan. 404 → none yet."""
    row = _latest_test_row(db, change_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no test plan yet")
    try:
        report = json.loads(row.content)
    except json.JSONDecodeError:
        logger.error("test report %s has invalid JSON content", row.id)
        raise HTTPException(status_code=500, detail="stored report is unreadable")
    return {
        "change_id": change_id,
        "version": row.version,
        "generated_at": row.generated_at.isoformat(),
        "profile_version": row.profile_version,
        "model_used": row.model_used,
        "report": report,
    }


@router.get("/changes/{change_id}/test/plan.docx")
def download_test_docx(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Render the latest test plan's markdown body as a .docx download."""
    row = _latest_test_row(db, change_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no test plan yet")
    try:
        report = json.loads(row.content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="stored report is unreadable")

    body_md = (report.get("test_plan_markdown") or "").strip()
    if not body_md:
        raise HTTPException(status_code=404, detail="test plan has no document body")

    title = f"Test plan — {report.get('one_line_summary', '')[:80]}"
    docx = markdown_to_docx_bytes(body_md, title=title)
    fname = f"test_plan_{change_id}_v{row.version}.docx"
    return Response(
        content=docx,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{urlquote(fname)}"'},
    )
