# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Feasibility analyser API routes.

Endpoints:
  GET  /api/feasibility/profile/status         — wiring sanity check (Slice 1)
  POST /api/feasibility/analyse/{change_id}    — run the analyser, persist a new report version (Slice 2)
  GET  /api/feasibility/report/{change_id}     — latest persisted report for this change (Slice 2)
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import registry
from app.api.auth import get_current_user
from app.database import get_db
from app.models import ChangeDocument, FeasibilityReport, IncomingChange, PartnerSetting

logger = logging.getLogger(__name__)

# Auth is attached at the ROUTER, not per-route (SAST F-003/F-004/F-005).
# All three endpoints here were unauthenticated while every comparable
# `api/dashboard/*.py` route required `get_current_user`; `main.py` mounts this
# router with no dependency of its own, so the gap was total. Router-level means
# a route added to this file later is covered by default — the per-route spelling
# is what let the boundary be forgotten in the first place.
router = APIRouter(
    prefix="/api/feasibility",
    tags=["feasibility"],
    dependencies=[Depends(get_current_user)],
)


# ── Slice 1 — wiring sanity check ───────────────────────────────────────────


@router.get("/profile/status")
def profile_status():
    """Is a partner profile configured? Returns metadata only (source / exists /
    size / parsed frontmatter) — never the full content.

    Reads the active (DB) profile via the shared loader, falling back to the
    mounted file — so this matches exactly what the analyser will use.
    """
    from app.agents._common import read_partner_profile

    raw, frontmatter = read_partner_profile()
    if not raw:
        return {
            "path": "db",
            "exists": False,
            "size_bytes": 0,
            "frontmatter": None,
        }
    return {
        "path": "db",
        "exists": True,
        "size_bytes": len(raw.encode("utf-8")),
        "frontmatter": frontmatter or None,
    }


# ── Slice 2 — run analyser + fetch report ───────────────────────────────────


def _resolve_runtime_api_key(db: Session) -> str | None:
    """Per partner-side convention, the Anthropic key may be pasted into
    `partner_settings` via the Settings UI rather than baked into the env.
    When present, the LLM call layer prefers it. Returns None if the
    setting isn't installed (caller falls back to env)."""
    row = db.execute(
        select(PartnerSetting).where(PartnerSetting.key == "partner_anthropic_api_key")
    ).scalar_one_or_none()
    return row.value if row and row.value else None


@router.post("/analyse/{change_id}")
def run_analysis(change_id: str, db: Session = Depends(get_db)):
    """Run the feasibility analyser against PARTNER.md + this change's
    documents. Persists a new `feasibility_reports` row (version = prev+1)
    and returns the parsed report.

    Idempotency: each call creates a NEW version row — re-runs do not
    overwrite prior outputs. Callers wanting "latest" use the GET endpoint.

    Requires an authenticated partner user (router-level dependency): this
    call spends the partner's Anthropic budget, so leaving it open was a
    cost/DoS surface as well as a broken-access-control one.
    """
    change = db.execute(
        select(IncomingChange).where(IncomingChange.id == change_id)
    ).scalar_one_or_none()
    if change is None:
        raise HTTPException(status_code=404, detail=f"unknown change_id: {change_id}")

    # Precondition: a partner profile must be configured. Without one the
    # analyser can only fail (or silently impersonate whatever happens to be
    # mounted), so surface a clear, actionable 400 instead of the generic 502
    # the agent would otherwise raise. The loader is DB-first with file fallback.
    from app.agents._common import read_partner_profile
    profile_raw, _ = read_partner_profile()
    if not profile_raw.strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "No partner profile configured. Upload or set your PARTNER.md in "
                "Settings → Partner Profile before running the feasibility analyser."
            ),
        )

    api_key = _resolve_runtime_api_key(db)

    # Version-aware context: v1 baseline documents + an LLM summary of what
    # changed in later versions (option C). Analyse the evolution, not a flat
    # dump of every version the partner holds.
    from app.agents.revision_context import assemble_change_context
    ctx = assemble_change_context(db, change_id, api_key)

    # Route through the agent registry so the run is audited in `agent_runs`
    # and respects the manifest binding (in-process today; a bank can repoint
    # feasibility at a remote service without changing this endpoint).
    agent_input = {
        "change_title": change.title,
        "change_initial_prompt": change.initial_prompt,
        "change_enhanced_prompt": change.enhanced_prompt,
        "documents": ctx["documents"],
        "revision_summary": ctx["revision_summary"],
        "api_key": api_key,
        "change_id": change_id,
    }
    try:
        report = registry.get("feasibility").execute(agent_input, db=db, change_id=change_id)
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller + audited
        raise HTTPException(
            status_code=502,
            detail=(
                "analyser failed — check logs. Common causes: PARTNER.md not "
                "mounted, no change documents, missing LLM provider key, or "
                f"model returned non-JSON output. ({exc})"
            ),
        )

    # Persist. Version = (max existing) + 1.
    prior_versions = db.execute(
        select(FeasibilityReport.version).where(FeasibilityReport.change_id == change_id)
    ).scalars().all()
    next_version = (max(prior_versions) + 1) if prior_versions else 1

    meta = report.get("_meta", {})
    row = FeasibilityReport(
        change_id=change_id,
        version=next_version,
        content=json.dumps(report),
        profile_version=meta.get("profile_version"),
        model_used=meta.get("model_used"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {
        "change_id": change_id,
        "version": row.version,
        "generated_at": row.generated_at.isoformat(),
        "report": report,
    }


@router.get("/report/{change_id}")
def get_latest_report(change_id: str, db: Session = Depends(get_db)):
    """Return the highest-version persisted report for this change.
    404 when no report exists yet — UI uses this to show the "run analyser"
    affordance vs the rendered report."""
    row = db.execute(
        select(FeasibilityReport)
        .where(FeasibilityReport.change_id == change_id)
        .order_by(FeasibilityReport.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no report yet")

    try:
        report = json.loads(row.content)
    except json.JSONDecodeError:
        logger.error("feasibility report %s has invalid JSON content", row.id)
        raise HTTPException(status_code=500, detail="stored report is unreadable")

    return {
        "change_id": change_id,
        "version": row.version,
        "generated_at": row.generated_at.isoformat(),
        "profile_version": row.profile_version,
        "model_used": row.model_used,
        "report": report,
    }
