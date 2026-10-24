"""Dashboard domain: npci_status — reconcile our mirror against NPCI's record.

Everything else in this package reads the partner platform's OWN tables, which
are a mirror built from inbound A2A pushes. When a push is lost, or a local
write fails after NPCI already acked our send, the mirror drifts and nothing in
the UI reveals it — the partner just sees a stage that looks fine and is wrong.

This module pulls NPCI's authoritative snapshot
(`GET /api/a2a/status/changes/{id}`, see `backend/app/api/partner_status.py`),
derives the same view from our local rows, and returns both plus an explicit
list of the fields that disagree. The partner never has to eyeball two screens.

Read-only on both sides: nothing here writes to our DB or NPCI's. Reconciling
must never itself be the thing that changes state.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models import IncomingChange, PartnerUser, ProgressReport
from app.npci_client import fetch_npci_status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


# The 7-stage journey, mirrored from the frontend's LIFECYCLE_STAGES and from
# NPCI's `_STAGE_LABELS`. All three lists must stay in the same order — the
# comparison below is only meaningful if both sides name the stages identically.
_STAGES = ["communicated", "accepted", "design", "coding", "testing",
           "ready_for_cert", "certified"]

# NPCI assignment status → our `cert_status` vocabulary. Inverse of the mapping
# the NPCI executor applies on an inbound cert_status_update
# (received→RECEIVED, deployed→APPLIED, tested→TESTED,
#  ready_for_certification→READY_FOR_CERTIFICATION). Anything at or past
# ready_for_certification collapses to that value — our column has no richer
# state to hold `certifying` / `in_production`.
_ASSIGNMENT_TO_CERT_STATUS = {
    "assigned":                "received",
    "received":                "received",
    "accepted":                "received",
    "applied":                 "deployed",
    "tested":                  "tested",
    "ready_for_certification": "ready_for_certification",
    "certifying":              "ready_for_certification",
    "certified":               "ready_for_certification",
    "ready_for_production":    "ready_for_certification",
    "in_production":           "ready_for_certification",
}


def _local_view(change: IncomingChange, steps: set[str]) -> dict:
    """Our own record, shaped like NPCI's snapshot so the two can be diffed."""
    done = {
        "communicated":   True,
        "accepted":       change.decision == "accepted",
        "design":         "design_completed" in steps,
        "coding":         "coding_completed" in steps,
        "testing":        "testing_completed" in steps,
        "ready_for_cert": change.cert_status in ("ready_for_certification", "certified")
                          or change.status in ("ready", "ready_for_certification", "certified"),
        "certified":      change.cert_status == "certified" or change.status == "certified",
    }
    current = "communicated"
    for key in _STAGES:
        if done[key]:
            current = key
    return {
        "stage":               {"current": current, "steps": done},
        "status":              change.status,
        "decision":            change.decision,
        "cert_status":         change.cert_status,
        "negotiation_version": change.negotiation_version,
        "frozen":              change.negotiation_finalized_at is not None,
        "progress_steps":      sorted(steps),
    }


def _diff(local: dict, npci: dict) -> list[dict]:
    """Fields where the two records disagree, most actionable first.

    Each entry is self-contained — field, both values, and what the partner
    should do about it — so the UI renders the list without interpreting it.
    """
    out: list[dict] = []
    npci_stage  = (npci.get("stage") or {})
    npci_steps  = (npci.get("progress_steps") or {})
    npci_neg    = (npci.get("negotiation") or {})
    npci_assign = (npci.get("assignment") or {})
    npci_status = npci_assign.get("status") or ""
    npci_done   = {s.get("key"): bool(s.get("done")) for s in (npci_stage.get("steps") or [])}

    # 1. Overall stage — the headline number the partner reports internally.
    if local["stage"]["current"] != npci_stage.get("current"):
        out.append({
            "field": "stage", "label": "Current stage",
            "local": local["stage"]["current"], "npci": npci_stage.get("current"),
            "severity": "mismatch",
            "hint": "NPCI is tracking a different stage. The per-milestone rows "
                    "below usually show which update went missing.",
        })

    # 2. Milestones — the classic lost update: we reported it, NPCI never
    #    recorded it (or the reverse, after an NPCI-side operator action).
    for step in ("design_completed", "coding_completed", "testing_completed"):
        ours   = step in local["progress_steps"]
        theirs = bool(npci_steps.get(step))
        if ours == theirs:
            continue
        out.append({
            "field": step, "label": step.replace("_", " ").title(),
            "local": "reported" if ours else "not reported",
            "npci":  npci_steps.get(step) or "not recorded",
            "severity": "mismatch",
            "hint": ("We reported this milestone but NPCI has no record of it — "
                     "re-submit it from the Progress panel."
                     if ours else
                     "NPCI has recorded this milestone but we haven't — our copy "
                     "is stale, not NPCI's."),
        })

    # 3. Acceptance — gates everything downstream, so a disagreement here is
    #    never cosmetic.
    ours_accepted   = local["stage"]["steps"]["accepted"]
    theirs_accepted = npci_done.get("accepted", False)
    if ours_accepted != theirs_accepted:
        out.append({
            "field": "accepted", "label": "Change accepted",
            "local": "accepted" if ours_accepted else "not accepted",
            "npci":  "accepted" if theirs_accepted else "not accepted",
            "severity": "mismatch",
            "hint": "Acceptance gates implementation on NPCI's side. If they show "
                    "us as not accepted, re-send the acceptance from Decision.",
        })

    # 4. Cert lifecycle.
    expected_cert = _ASSIGNMENT_TO_CERT_STATUS.get(npci_status)
    if expected_cert and local["cert_status"] != expected_cert:
        out.append({
            "field": "cert_status", "label": "Certification status",
            "local": local["cert_status"], "npci": expected_cert,
            "severity": "mismatch",
            "hint": f"NPCI's assignment status is '{npci_status}'. Re-send the cert "
                    f"status update if ours is ahead of theirs.",
        })

    # 5. Kit version — if NPCI is on a higher version, a kit dispatch never
    #    reached us and we are reading a superseded spec.
    if local["negotiation_version"] != npci_neg.get("version"):
        ahead = (npci_neg.get("version") or 0) > local["negotiation_version"]
        out.append({
            "field": "negotiation_version", "label": "Product Kit version",
            "local": local["negotiation_version"], "npci": npci_neg.get("version"),
            "severity": "mismatch",
            "hint": ("NPCI has published a newer kit version than we hold — we are "
                     "working off a superseded spec. Ask NPCI to re-dispatch it."
                     if ahead else
                     "We hold a higher version than NPCI's record shows — flag this "
                     "with NPCI before acting on it."),
        })

    # 6. Freeze — determines whether counter-proposals are still accepted.
    if local["frozen"] != bool(npci_neg.get("frozen")):
        out.append({
            "field": "frozen", "label": "Negotiation frozen",
            "local": "frozen" if local["frozen"] else "open",
            "npci":  "frozen" if npci_neg.get("frozen") else "open",
            "severity": "mismatch",
            "hint": "Once NPCI freezes, counter-proposals are rejected and only "
                    "emergency issues are accepted.",
        })

    # 7. Blocked flag — NPCI-side only; we have no local mirror for it, so this
    #    is surfaced as information rather than a disagreement.
    if npci_assign.get("blocked"):
        out.append({
            "field": "blocked", "label": "Blocked at NPCI",
            "local": "—", "npci": npci_assign.get("blocked_reason") or "blocked",
            "severity": "info",
            "hint": "NPCI has flagged this assignment as blocked on their side.",
        })

    return out


@router.get("/changes/{change_id}/npci-status")
def get_npci_status(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """NPCI's record for this change, ours, and where they disagree.

    Called on demand from the change page ("Verify with NPCI"), never on a
    poll — it costs an auth handshake plus a round trip to NPCI, and the
    answer is only interesting when someone is actually questioning the state.
    """
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    try:
        npci = fetch_npci_status(db, change.npci_change_id)
    except RuntimeError as exc:
        # 502, not 500: the partner platform is fine, the upstream answer isn't.
        # The UI shows `detail` verbatim — it already reads as operator advice.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    steps = {
        p.step for p in db.scalars(
            select(ProgressReport).where(ProgressReport.change_id == change_id)
        ).all()
    }
    local = _local_view(change, steps)
    discrepancies = _diff(local, npci)

    logger.info(
        "NPCI reconcile: change=%s local_stage=%s npci_stage=%s discrepancies=%d",
        change_id, local["stage"]["current"],
        (npci.get("stage") or {}).get("current"), len(discrepancies),
    )

    return {
        "change_id":     change_id,
        "npci_change_id": change.npci_change_id,
        "checked_at":    npci.get("as_of"),
        "in_sync":       not any(d["severity"] == "mismatch" for d in discrepancies),
        "discrepancies": discrepancies,
        "local":         local,
        "npci":          npci,
    }
