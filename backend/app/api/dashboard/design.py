# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: design — run the partner Design agent against a change's
product-kit documents, persist a versioned design document, serve it (JSON +
.docx download).

Mirrors the feasibility flow (app/api/feasibility.py) but lives in the
authenticated dashboard package: both endpoints require get_current_user, and
the design doc is downloadable as .docx via the shared markdown renderer.
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
from app.models import DesignReport, FeasibilityReport, IncomingChange, PartnerSetting, PartnerUser
from app.rag.retrieval import build_kb_context

from ._shared import markdown_to_docx_bytes

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


def _kb_context(db: Session, change: IncomingChange) -> str:
    """Retrieve partner knowledge-base context relevant to this change. Fail-soft
    → "" so the agent still runs when the RAG store is unavailable."""
    query = f"{change.title or ''} {(change.enhanced_prompt or change.initial_prompt or '')[:500]}".strip()
    return build_kb_context(db, query)


def _runtime_api_key(db: Session) -> str | None:
    """Anthropic key pasted into partner_settings via the Settings UI, if any."""
    row = db.get(PartnerSetting, "partner_anthropic_api_key")
    return row.value if row and row.value else None


def _latest_feasibility_summary(db: Session, change_id: str) -> str | None:
    """The newest feasibility one-liner, so design builds on 'can we' → 'how'."""
    row = db.execute(
        select(FeasibilityReport)
        .where(FeasibilityReport.change_id == change_id)
        .order_by(FeasibilityReport.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not row:
        return None
    try:
        return (json.loads(row.content) or {}).get("one_line_summary")
    except (json.JSONDecodeError, TypeError):
        return None


def _run_design_job(change_id: str, db: Session, progress) -> None:
    """The actual design run — executes inside a background AgentJob (jobs.py).
    Raises on failure (the job row records str(exc))."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise RuntimeError(f"unknown change_id: {change_id}")

    api_key = _runtime_api_key(db)

    # Version-aware context: v1 baseline + an LLM summary of later-version deltas.
    progress("assembling change context")
    from app.agents.revision_context import assemble_change_context
    ctx = assemble_change_context(db, change_id, api_key)

    agent_input = {
        "change_title": change.title,
        "change_initial_prompt": change.initial_prompt,
        "change_enhanced_prompt": change.enhanced_prompt,
        "documents": ctx["documents"],
        "revision_summary": ctx["revision_summary"],
        "feasibility_summary": _latest_feasibility_summary(db, change_id),
        "knowledge_context": _kb_context(db, change),
        "api_key": api_key,
        "change_id": change_id,
    }
    progress("generating design document (takes a few minutes)")
    try:
        report = registry.get("design").execute(agent_input, db=db, change_id=change_id)
    except Exception as exc:  # noqa: BLE001 — surfaced on the job row + audited in agent_runs
        raise RuntimeError(
            "design agent failed — check logs. Common causes: PARTNER.md not "
            "mounted, no change documents, missing LLM provider key, or model "
            f"returned non-JSON. ({exc})"
        )

    prior = db.execute(
        select(DesignReport.version).where(DesignReport.change_id == change_id)
    ).scalars().all()
    next_version = (max(prior) + 1) if prior else 1

    meta = report.get("_meta", {})
    row = DesignReport(
        change_id=change_id,
        version=next_version,
        content=json.dumps(report),
        profile_version=meta.get("profile_version"),
        model_used=meta.get("model_used"),
    )
    db.add(row)
    db.commit()


@router.post("/changes/{change_id}/design/analyse", status_code=202)
def run_design(
    change_id: str,
    bg: BackgroundTasks,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Kick a background Design-agent run; returns 202 + a job id immediately.
    Poll GET /changes/{id}/jobs/design/latest for state; the report lands in
    design_reports (GET /changes/{id}/design/report) when the job is done."""
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise HTTPException(status_code=404, detail=f"unknown change_id: {change_id}")
    from .jobs import start_job
    return start_job(
        db, bg, change_id=change_id, kind="design",
        runner=lambda jdb, progress: _run_design_job(change_id, jdb, progress),
    )


def _latest_design_row(db: Session, change_id: str) -> DesignReport | None:
    return db.execute(
        select(DesignReport)
        .where(DesignReport.change_id == change_id)
        .order_by(DesignReport.version.desc())
        .limit(1)
    ).scalar_one_or_none()


@router.get("/changes/{change_id}/design/report")
def get_design_report(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the highest-version persisted design report. 404 → none yet (UI
    shows the 'run design' affordance)."""
    row = _latest_design_row(db, change_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no design report yet")
    try:
        report = json.loads(row.content)
    except json.JSONDecodeError:
        logger.error("design report %s has invalid JSON content", row.id)
        raise HTTPException(status_code=500, detail="stored report is unreadable")
    return {
        "change_id": change_id,
        "version": row.version,
        "generated_at": row.generated_at.isoformat(),
        "profile_version": row.profile_version,
        "model_used": row.model_used,
        "report": report,
    }


@router.get("/changes/{change_id}/design/document.docx")
def download_design_docx(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Render the latest design document's markdown body as a .docx download."""
    row = _latest_design_row(db, change_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no design report yet")
    try:
        report = json.loads(row.content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="stored report is unreadable")

    body_md = (report.get("document_markdown") or "").strip()
    if not body_md:
        raise HTTPException(status_code=404, detail="design report has no document body")

    title = f"Design — {report.get('one_line_summary', '')[:80]}"
    docx = markdown_to_docx_bytes(body_md, title=title)
    fname = f"design_{change_id}_v{row.version}.docx"
    return Response(
        content=docx,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{urlquote(fname)}"'},
    )
