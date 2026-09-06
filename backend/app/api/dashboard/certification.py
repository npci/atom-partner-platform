# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: certification — cert lifecycle status + readiness declaration."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.core.test_status import TERMINAL_STATUSES, TestStatus, normalise
from app.database import get_db
from app.models import ChangeDocument, ChangeTestData, IncomingChange, PartnerSetting, PartnerUser, ProgressReport
from app.npci_client import declare_ready
from app.agents.test_data_suggester import suggest_test_data

from ._shared import _CERT_STATUS_ORDER

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


class CertStatusRequest(BaseModel):
    """Body for POST /changes/{id}/cert-status.

    `status` is one of: deployed | tested | ready_for_certification.
    `received` is auto-set on change_communication and is not advanced
    via this endpoint. The final 'ready_for_certification' status
    additionally accepts role + test_data so the NPCI cert orchestrator
    can configure cert-agent test cases.
    """
    status:    str
    role:      str | None = None
    test_data: dict | None = None


@router.get("/changes/{change_id}/cert-status")
def get_cert_status(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the cert lifecycle state + history for the lifecycle UI."""
    import json as _json
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    history = {}
    if change.cert_status_history:
        try:
            history = _json.loads(change.cert_status_history) or {}
        except Exception:
            history = {}
    # Decoded cert_test_response summary, if NPCI has published one
    # back for this change. Shape: {total, passed, failed, cases:[...]}.
    cert_summary = None
    if getattr(change, "cert_summary", None):
        try:
            cert_summary = _json.loads(change.cert_summary)
        except Exception:
            cert_summary = None
    return {
        "change_id":    change_id,
        "current":      change.cert_status or "received",
        "history":      history,
        "order":        _CERT_STATUS_ORDER,
        "cert_summary": cert_summary,
        # The NPCI Certification Result certificate, if the all-PASS sign-off
        # has arrived. Drives the download button on the Certified stage.
        "has_signoff":      bool(getattr(change, "cert_signoff_docx_bytes", None)),
        "signoff_filename": getattr(change, "cert_signoff_filename", None),
    }


@router.post("/changes/{change_id}/cert-status")
def update_cert_status(
    change_id: str,
    body: CertStatusRequest,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Advance the cert lifecycle and notify NPCI via A2A.

    Enforces the linear order: each status can only advance to the next
    one (or repeat the current one if needed). The first state
    'received' is auto-set on change receipt and cannot be advanced to
    here.
    """
    import json as _json
    from datetime import datetime, timezone

    from app.npci_client import send_cert_status_update

    target = (body.status or "").strip().lower()
    if target not in _CERT_STATUS_ORDER or target == "received":
        raise HTTPException(
            status_code=400,
            detail="Invalid target status. Allowed: deployed | tested | ready_for_certification",
        )

    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    current_idx = _CERT_STATUS_ORDER.index(change.cert_status or "received")
    target_idx  = _CERT_STATUS_ORDER.index(target)
    if target_idx != current_idx + 1:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot transition from '{change.cert_status}' to '{target}'. "
                f"Linear flow: {' → '.join(_CERT_STATUS_ORDER)}"
            ),
        )

    role      = (body.role or "").strip()
    test_data = body.test_data or {}
    if target == "ready_for_certification" and not role:
        raise HTTPException(
            status_code=400,
            detail="role is required when declaring readiness for certification",
        )

    # Persist locally first so the UI reflects the transition even if
    # the A2A send fails transiently.
    history = {}
    if change.cert_status_history:
        try:
            history = _json.loads(change.cert_status_history) or {}
        except Exception:
            history = {}
    history[target] = datetime.now(timezone.utc).isoformat()
    change.cert_status = target
    change.cert_status_history = _json.dumps(history)
    db.commit()

    # Slice 3 — bundle every saved ChangeTestData row as
    # `test_data_per_case` keyed by tc_id when the partner declares
    # readiness. NPCI's orchestrator merges these per-TC during PATCH.
    test_data_per_case: dict = {}
    if target == "ready_for_certification":
        rows = db.scalars(
            select(ChangeTestData).where(ChangeTestData.change_id == change_id)
        ).all()
        for r in rows:
            if r.test_data:
                test_data_per_case[r.tc_id] = r.test_data
        logger.info(
            "Cert ready_for_certification: change=%s per_case_rows=%d",
            change_id, len(test_data_per_case),
        )

    # Protocol v1: deployed/tested are implementation progress already tracked
    # by milestone_update — don't duplicate them on the cert wire. Only
    # ready_for_certification goes to NPCI (it carries role + test_data and
    # triggers the cert run). deployed/tested update local status only.
    sent = None
    if target == "ready_for_certification":
        sent = send_cert_status_update(
            db, change.npci_change_id, target,
            role=role, test_data=test_data,
            test_data_per_case=test_data_per_case or None,
        )
    logger.info(
        "Cert status updated: change=%s status=%s role=%s per_case=%d sent=%s",
        change_id, target, role or "-", len(test_data_per_case), bool(sent),
    )
    return {
        "current":           target,
        "sent_to_npci":      bool(sent),
        "history":           history,
        "per_case_tc_count": len(test_data_per_case),
    }


class CertMockCompleteRequest(BaseModel):
    """Body for POST /changes/{id}/cert-mock-complete.

    Demo-mode endpoint used by the partner UI when NPCI's cert orchestrator
    isn't publishing results back on this environment (dev / demo). The
    frontend synthesises a UPI-style result set and posts it here so the
    completion persists across page reloads and (best-effort) reaches the
    NPCI side over A2A.

    `cert_summary` is optional on re-calls: when the change is already
    locally certified and the body omits it (empty dict), the endpoint
    resends the previously-saved summary to NPCI. This makes the endpoint
    self-healing when the first A2A push was rejected (e.g. old NPCI code
    without the mock-synth handler) — a subsequent call will retry the
    push without touching local state.
    """
    cert_summary: dict = {}


@router.post("/changes/{change_id}/cert-mock-complete")
def cert_mock_complete(
    change_id: str,
    body: CertMockCompleteRequest,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Persist a mocked cert run as certified locally and (re-)notify NPCI.

    Local persistence is idempotent (re-writing the same certified state is a
    no-op). The A2A push to NPCI runs on every call so a partner that certified
    locally before NPCI knew how to accept the synthetic run can still resync
    the assignment status to CERTIFIED on the next call. NPCI's
    process_cert_test_response is itself idempotent — it deletes prior results
    for the same cert_run_id before rewriting them and set_status is
    forward-only guarded.
    """
    import json as _json
    from datetime import datetime, timezone

    from app.npci_client import send_task

    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    already_certified = change.cert_status == "certified" and bool(change.cert_summary)

    # Resolve the effective summary. On a re-call with an empty body, reuse
    # the previously-persisted summary so this endpoint can be used as a
    # zero-arg resync trigger from the frontend once the change is certified.
    if body.cert_summary:
        summary = body.cert_summary
    elif already_certified:
        try:
            summary = _json.loads(change.cert_summary) or {}
        except Exception:  # noqa: BLE001
            summary = {}
    else:
        raise HTTPException(
            status_code=400,
            detail="cert_summary is required on first mock-complete (change not yet certified).",
        )

    # Persist local state only when this is the first-time certification —
    # nothing to write on a pure resync call.
    if not already_certified:
        change.cert_summary = _json.dumps(summary)
        history = {}
        if change.cert_status_history:
            try:
                history = _json.loads(change.cert_status_history) or {}
            except Exception:
                history = {}
        certified_at = datetime.now(timezone.utc).isoformat()
        history.setdefault("certified", certified_at)
        change.cert_status = "certified"
        change.cert_status_history = _json.dumps(history)
        db.commit()
    else:
        # Preserve the original certified timestamp for the wire payload.
        history = {}
        if change.cert_status_history:
            try:
                history = _json.loads(change.cert_status_history) or {}
            except Exception:
                history = {}
        certified_at = history.get("certified") or datetime.now(timezone.utc).isoformat()

    # Best-effort mirror to NPCI. The wire type mirrors the real
    # cert_test_response NPCI would publish itself when the orchestrator
    # completes; the payload carries the same fields so the NPCI-side
    # UI can pick up the run + per-TC breakdown. If NPCI rejects (e.g.
    # unknown synthetic cert_run_id), we log and continue — the partner
    # side is already correctly certified.
    cases = summary.get("cases") or []
    def _dir_for(initiated_by: str) -> str:
        # NPCI-side `_normalize_direction` accepts "partner_to_npci" (BANK-
        # initiated round-trips) and treats anything else as
        # "npci_to_partner". Map the display value accordingly.
        return "partner_to_npci" if str(initiated_by or "").upper() == "BANK" else "npci_to_partner"
    results = [
        {
            "test_case_id":     c.get("test_case_id") or c.get("tc_id"),
            "status":           normalise(c.get("status")),
            "expected_response":c.get("expected_code"),
            "actual_response":  c.get("actual_code"),
            "expected_code":    c.get("expected_code"),
            "actual_code":      c.get("actual_code"),
            "scenario":         c.get("scenario"),
            "initiated_by":     c.get("initiated_by") or "NPCI",
            "direction":        _dir_for(c.get("initiated_by")),
            "api":              c.get("api"),
            "title":            c.get("title"),
            "duration_ms":      c.get("duration_ms"),
            "latency_ms":       c.get("duration_ms"),
            "remarks":          c.get("remarks"),
        }
        for c in cases
    ]
    # Named outcome constants rather than bare "PASS"/"FAIL" literals — see
    # core/test_status.py (these are test results, not credentials).
    passed  = sum(1 for r in results if r["status"] == TestStatus.PASS)
    failed  = sum(1 for r in results if r["status"] == TestStatus.FAIL)
    skipped = sum(1 for r in results if r["status"] not in TERMINAL_STATUSES)
    wire = {
        "cert_run_id":     f"mock-{change_id}",
        "external_run_id": f"mock-run-{change_id}",
        "feature_name":    summary.get("feature") or "UPI Circle Personalization",
        "flow":            summary.get("feature") or "UPI Circle Personalization",
        "role":            summary.get("role"),
        "total":           len(results),
        "passed":          passed,
        "failed":          failed,
        "skipped":         skipped,
        "completed_at":    certified_at,
        "results":         results,
        "phases":          summary.get("phases"),
        "mock":            True,
        "source":          "partner_demo",
    }
    sent = None
    try:
        sent = send_task(db, "cert_test_response", change.npci_change_id, wire)
    except Exception:  # noqa: BLE001
        logger.warning(
            "cert_mock_complete: best-effort A2A push to NPCI failed change=%s",
            change_id, exc_info=True,
        )

    logger.info(
        "cert_mock_complete: change=%s cases=%d passed=%d failed=%d sent=%s",
        change_id, len(results), passed, failed, bool(sent),
    )
    return {
        "current":      "certified",
        "sent_to_npci": bool(sent),
        "cases":        len(results),
        "history":      history,
        "resynced":     already_certified,
    }


@router.get("/changes/{change_id}/cert-signoff.pdf")
def download_cert_signoff_pdf(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Render the NPCI Certification Sign-off certificate as a PDF.

    Sourced from `incoming_changes.cert_summary` + partner profile. Only
    available once cert_status has advanced to 'certified' — otherwise
    a 409 signals the UI to hide the download link.
    """
    import json as _json
    from fastapi.responses import Response

    from app.config import settings
    from app.models import PartnerProfile, PartnerSetting
    from app.services.cert_signoff_pdf import build_signoff_pdf

    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    if change.cert_status != "certified" or not change.cert_summary:
        raise HTTPException(
            status_code=409,
            detail="Certification sign-off is only available after the cert run has completed as certified.",
        )
    try:
        summary = _json.loads(change.cert_summary) or {}
    except Exception:  # noqa: BLE001
        summary = {}

    history = {}
    if change.cert_status_history:
        try:
            history = _json.loads(change.cert_status_history) or {}
        except Exception:
            history = {}
    executed_at = history.get("certified") or summary.get("completed_at")

    # The bank certifying the change: prefer the operator-editable profile row
    # (Settings UI seeds this from PARTNER.md frontmatter → e.g. "SBI"), then
    # the legacy KV setting, then the env-driven default. Only fall through to
    # the generic literal if the deployment has no identity configured at all.
    profile = db.query(PartnerProfile).first()
    partner_name_row = db.get(PartnerSetting, "partner_name")
    partner_name = (
        (profile.partner_name if profile and profile.partner_name else None)
        or (partner_name_row.value if partner_name_row else None)
        or (settings.partner_name if settings.partner_name and settings.partner_name != "Partner Agent" else None)
        or "Partner Bank"
    )

    pdf_bytes = build_signoff_pdf(
        partner_name = partner_name,
        feature      = summary.get("feature") or "UPI Product",
        run_id       = summary.get("cert_run_id") or f"CERT-{change_id[:8].upper()}",
        executed_at  = executed_at,
        role         = summary.get("role"),
        test_data    = summary.get("test_data") or {},
        cases        = summary.get("cases") or [],
        summary      = summary,
    )
    filename = f"NPCI_Certification_Signoff_{change.npci_change_id or change_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ReadinessRequest(BaseModel):
    """Test data the partner ships with their readiness declaration so
    NPCI's cert orchestrator can pre-configure cert-agent test cases.

    `role` selects which subset of test cases (by tc_id prefix
    PR_/PE_/RE_/BE_) gets this partner's data injected. `test_data` is a
    flat dict of role-relevant fields; only keys relevant to the role
    are read by the NPCI orchestrator (payer_vpa for PAYER_PSP,
    payee_vpa for PAYEE_PSP, account_number/ifsc for bank roles).

    All fields are optional — when omitted the request degrades to the
    legacy `{status: ready_for_cert}` payload for back-compat with
    older partner UIs.
    """
    role:      str | None = None  # PAYER_PSP | PAYEE_PSP | REMITTER_BANK | BENEFICIARY_BANK
    test_data: dict | None = None


@router.post("/changes/{change_id}/ready")
def submit_readiness(
    change_id: str,
    body: ReadinessRequest | None = None,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    # Check all steps completed
    existing = db.scalars(select(ProgressReport).where(ProgressReport.change_id == change_id)).all()
    reported = {p.step for p in existing}
    for step in ["design_completed", "coding_completed", "testing_completed"]:
        if step not in reported:
            raise HTTPException(status_code=400, detail=f"Step '{step}' not yet reported")

    role      = (body.role if body else None) or ""
    test_data = (body.test_data if body else None) or {}

    result = declare_ready(db, change.npci_change_id, role=role, test_data=test_data)
    change.status = "ready"
    db.commit()

    logger.info(
        "Readiness declared: change=%s role=%s test_data_keys=%s",
        change_id, role, list(test_data.keys()),
    )
    return {"declared": True, "sent_to_npci": result is not None, "role": role}


# ── Per-Case Test Data (Slice 2) ──────────────────────────────────────────────
#
# Discovery: parse the cert_test_cases ChangeDocument's markdown table to
# enumerate TC IDs + their scenarios. NPCI-initiated TCs are what need
# bank-side test data; bank-initiated TCs drive themselves on the bank's
# own simulator so they're elided from the form.


def _parse_cert_test_cases_md(content: str) -> list[dict]:
    """Pull TC rows out of a cert_test_cases markdown table.

    Returns a list of `{tc_id, scenario, expected, initiated_by, api}` dicts.
    Mirrors the parser the NPCI cert-push endpoint uses so partner and
    NPCI agree on what the source said.
    """
    rows: list[dict] = []
    headers: list[str] = []
    for ln in (content or "").splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            headers = []
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if not headers:
            headers = [c.lower() for c in cells]
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        row = dict(zip(headers, cells + [""] * (len(headers) - len(cells))))
        tid = row.get("tc id") or row.get("id") or row.get("test id") or ""
        if not tid:
            continue
        init_raw = (
            row.get("initiated by") or row.get("initiator")
            or row.get("initiated") or row.get("trigger") or ""
        ).strip().upper()
        if init_raw in ("BANK", "B"):
            initiated_by = "BANK"
        elif init_raw in ("NPCI", "N"):
            initiated_by = "NPCI"
        else:
            initiated_by = ""  # unknown — UI defaults to NPCI
        # I-8: the outbound flow a BANK-initiated case originates. OPTIONAL —
        # the canonical `| TC ID | Scenario | Expected |` source carries no
        # such column, and both this parser and NPCI's cert-push one
        # deliberately ignore columns they do not recognise. Left empty when
        # absent so the trigger codegen emits NOTHING rather than guessing a
        # flow out of the scenario prose: a handler wired to a guessed flow
        # would originate the wrong call and report it as the right one.
        api = (
            row.get("api") or row.get("flow")
            or row.get("api name") or row.get("api type") or ""
        ).strip()
        rows.append({
            "tc_id":        tid,
            "scenario":     row.get("scenario") or row.get("description") or "",
            "expected":     row.get("expected") or row.get("result") or "",
            "initiated_by": initiated_by,
            "api":          api,
        })
    return rows


def case_flows_for_change(db: Session, change_id: str) -> dict[str, str]:
    """`test_case_id -> outbound flow` for the change's BANK-initiated cases.

    The DB half of I-8's `case_flow_map`, which is deliberately pure (it takes
    rows, not a session), so the ChangeDocument lookup lives here beside the
    parser whose output shape it adapts.

    Returns `{}` when the suite names no bank-initiated case, or names some but
    the source carries no api/flow column. The codegen turns that into "emit
    nothing", which is the honest outcome — `emit_trigger_handler`'s own rule is
    that a handler refusing every id is worse than no handler at all.
    """
    from app.services.cert_trigger_codegen import case_flow_map

    cert_doc = db.scalars(
        select(ChangeDocument).where(
            ChangeDocument.change_id == change_id,
            ChangeDocument.doc_type == "cert_test_cases",
        )
    ).first()
    return case_flow_map([
        {"case_id": r["tc_id"], "initiator": r["initiated_by"], "api": r["api"]}
        for r in _parse_cert_test_cases_md(cert_doc.content if cert_doc else "")
    ])


def _docs_context_for_change(db: Session, change_id: str) -> str:
    """Truncated concatenation of the change's BRD / cert_test_cases /
    product_note bodies. Fed to the LLM suggester so its values are
    informed by anything bank-specific the change docs leak."""
    interesting = {"brd", "cert_test_cases", "product_note", "manifest"}
    rows = db.scalars(
        select(ChangeDocument).where(ChangeDocument.change_id == change_id)
    ).all()
    parts: list[str] = []
    for r in rows:
        if (r.doc_type or "").lower() not in interesting:
            continue
        if not r.content:
            continue
        parts.append(f"--- {r.doc_type} ---\n{r.content[:5000]}")
    return "\n\n".join(parts)[:15000]


@router.get("/changes/{change_id}/test-data")
def list_test_data(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List every TC discovered from the cert_test_cases doc + any
    already-saved per-TC test data the bank entered.

    Bank-initiated TCs are returned too (with `initiated_by:'BANK'`) so
    the UI can show the full picture, but the form only nudges the user
    to fill the NPCI-initiated rows.
    """
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    cert_doc = db.scalars(
        select(ChangeDocument).where(
            ChangeDocument.change_id == change_id,
            ChangeDocument.doc_type == "cert_test_cases",
        )
    ).first()
    discovered = _parse_cert_test_cases_md(cert_doc.content if cert_doc else "")

    saved = {
        row.tc_id: row
        for row in db.scalars(
            select(ChangeTestData).where(ChangeTestData.change_id == change_id)
        ).all()
    }

    # Build the merged view: every discovered row, with its saved data
    # overlaid; plus any saved rows for TCs not in the discovered list
    # (handles the case where the bank entered a TC id by hand).
    merged: list[dict] = []
    discovered_ids = set()
    for d in discovered:
        discovered_ids.add(d["tc_id"])
        row = saved.get(d["tc_id"])
        merged.append({
            "tc_id":        d["tc_id"],
            "scenario":     d["scenario"],
            "expected":     d["expected"],
            "initiated_by": d["initiated_by"] or "NPCI",
            "test_data":    (row.test_data if row else {}),
            "ai_suggested": bool(row.ai_suggested) if row else False,
            "updated_at":   row.updated_at.isoformat() if row else None,
        })
    for tc_id, row in saved.items():
        if tc_id in discovered_ids:
            continue
        merged.append({
            "tc_id":        tc_id,
            "scenario":     "",
            "expected":     "",
            "initiated_by": "NPCI",
            "test_data":    row.test_data,
            "ai_suggested": bool(row.ai_suggested),
            "updated_at":   row.updated_at.isoformat(),
        })

    npci_cases = [c for c in merged if c["initiated_by"] != "BANK"]
    bank_cases = [c for c in merged if c["initiated_by"] == "BANK"]
    return {
        "change_id":          change_id,
        "cases":              merged,
        "npci_total":         len(npci_cases),
        "bank_total":         len(bank_cases),
        "npci_with_data":     sum(1 for c in npci_cases if c["test_data"]),
        "discovered_from_md": bool(cert_doc),
    }


class TestDataUpsert(BaseModel):
    test_data: dict
    ai_suggested: bool = False


@router.put("/changes/{change_id}/test-data/{tc_id}")
def upsert_test_data(
    change_id: str,
    tc_id: str,
    body: TestDataUpsert,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save per-TC test data the bank operator just edited.

    `ai_suggested=True` is reserved for one-shot writes via the
    LLM-suggest endpoint below; once the operator opens the form and
    saves, the flag goes false so the UI can mark it as human-curated.
    """
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")
    row = db.scalars(
        select(ChangeTestData).where(
            ChangeTestData.change_id == change_id,
            ChangeTestData.tc_id == tc_id,
        )
    ).first()
    if row:
        row.test_data    = body.test_data or {}
        row.ai_suggested = body.ai_suggested
    else:
        row = ChangeTestData(
            change_id=change_id,
            tc_id=tc_id,
            test_data=body.test_data or {},
            ai_suggested=body.ai_suggested,
        )
        db.add(row)
    db.commit()
    return {"tc_id": tc_id, "test_data": row.test_data, "ai_suggested": row.ai_suggested}


class SuggestRequest(BaseModel):
    scenario:        str | None = None  # override; falls back to md
    expected_status: str | None = None
    response_code:   str | None = None
    persist:         bool = True  # save the suggestion as a draft row


@router.post("/changes/{change_id}/test-data/{tc_id}/suggest")
def suggest_per_tc(
    change_id: str,
    tc_id: str,
    body: SuggestRequest | None = None,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """LLM-suggest test_data for a single TC. Returns the suggestion +
    persists it as `ai_suggested=True` unless `persist=False`.
    """
    change = db.get(IncomingChange, change_id)
    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    body = body or SuggestRequest()

    # If the caller didn't pass a scenario, look it up in the md doc
    scenario        = (body.scenario or "").strip()
    expected_status = (body.expected_status or "").strip()
    response_code   = (body.response_code or "").strip()
    if not scenario:
        cert_doc = db.scalars(
            select(ChangeDocument).where(
                ChangeDocument.change_id == change_id,
                ChangeDocument.doc_type == "cert_test_cases",
            )
        ).first()
        for d in _parse_cert_test_cases_md(cert_doc.content if cert_doc else ""):
            if d["tc_id"] == tc_id:
                scenario = d["scenario"]
                expected_status = expected_status or d["expected"]
                break

    api_key = None
    row = db.get(PartnerSetting, "partner_anthropic_api_key")
    if row:
        api_key = row.value

    result = suggest_test_data(
        api_key=api_key,
        change_title=change.title or "",
        tc_id=tc_id,
        scenario_summary=scenario,
        expected_status=expected_status,
        response_code=response_code,
        docs_context=_docs_context_for_change(db, change_id),
    )

    if body.persist and result.get("test_data"):
        existing = db.scalars(
            select(ChangeTestData).where(
                ChangeTestData.change_id == change_id,
                ChangeTestData.tc_id == tc_id,
            )
        ).first()
        if existing:
            existing.test_data    = result["test_data"]
            existing.ai_suggested = True
        else:
            db.add(ChangeTestData(
                change_id=change_id,
                tc_id=tc_id,
                test_data=result["test_data"],
                ai_suggested=True,
            ))
        db.commit()

    return {
        "tc_id":     tc_id,
        "test_data": result.get("test_data", {}),
        "rationale": result.get("rationale", ""),
        "persisted": body.persist and bool(result.get("test_data")),
    }


@router.delete("/changes/{change_id}/test-data/{tc_id}")
def delete_test_data(
    change_id: str,
    tc_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.scalars(
        select(ChangeTestData).where(
            ChangeTestData.change_id == change_id,
            ChangeTestData.tc_id == tc_id,
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="No test data saved for this TC")
    db.delete(row)
    db.commit()
    return {"deleted": True}
