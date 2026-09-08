# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Dashboard domain: certification — cert lifecycle status + readiness declaration."""
import logging
import re
from urllib.parse import quote as urlquote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.core.test_status import TERMINAL_STATUSES, TestStatus, normalise
from app.database import get_db
from app.core.llm import resolve_runtime_api_key
from app.models import (
    ChangeDocument, ChangeTestData, IncomingChange, OutboundA2ARetry,
    PartnerSetting, PartnerUser, ProgressReport,
)
from app.authority_client import declare_ready
from app.agents.test_data_suggester import suggest_test_data

from ._shared import _CERT_STATUS_ORDER

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["dashboard"])


class CertStatusRequest(BaseModel):
    """Body for POST /changes/{id}/cert-status.

    `status` is one of: deployed | tested | ready_for_certification.
    `received` is auto-set on change_communication and is not advanced
    via this endpoint. The final 'ready_for_certification' status
    additionally accepts role + test_data so the authority's cert orchestrator
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
    # Decoded cert_test_response summary, if the authority has published one
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
        # The authority's Certification Result certificate, if the all-PASS sign-off
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
    """Advance the cert lifecycle and notify the authority via A2A.

    Enforces the linear order: each status can only advance to the next
    one (or repeat the current one if needed). The first state
    'received' is auto-set on change receipt and cannot be advanced to
    here.
    """
    import json as _json
    from datetime import datetime, timezone

    from app.authority_client import send_cert_status_update

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

    # Bundle every saved ChangeTestData row as
    # `test_data_per_case` keyed by tc_id when the partner declares
    # readiness. The authority's orchestrator merges these per-TC during PATCH.
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
    # ready_for_certification goes to the authority (it carries role + test_data and
    # triggers the cert run). deployed/tested update local status only.
    sent = None
    if target == "ready_for_certification":
        sent = send_cert_status_update(
            db, change.authority_change_id, target,
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


@router.get("/changes/{change_id}/cert-signoff.pdf")
def download_cert_signoff_pdf(
    change_id: str,
    user: PartnerUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Render the Certification Sign-off certificate as a PDF.

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

    # The institution certifying the change: prefer the operator-editable profile
    # row (the Settings UI seeds this from PARTNER.md frontmatter), then the
    # legacy KV setting, then the env-driven default. Only fall through to the
    # generic literal if the deployment has no identity configured at all.
    profile = db.query(PartnerProfile).first()
    partner_name_row = db.get(PartnerSetting, "partner_name")
    partner_name = (
        (profile.partner_name if profile and profile.partner_name else None)
        or (partner_name_row.value if partner_name_row else None)
        or (settings.partner_name if settings.partner_name and settings.partner_name != "Partner Agent" else None)
        # Last-resort literal, reached only when the deployment has configured
        # no identity at all. Was "Partner Bank", which printed a financial
        # institution onto the sign-off of a library network that had simply
        # not filled in its profile.
        or "The Partner"
    )

    # Fallbacks come from the pack, not from a literal. build_signoff_pdf() was
    # made domain-general, but these three defaults live in its CALLER and were
    # missed on that pass — the same shape of bug as templating a system prompt
    # while the user prompt beside it keeps its hardcoded tokens.
    from app.core.domain import get_active_pack

    _pack = get_active_pack()

    pdf_bytes = build_signoff_pdf(
        partner_name = partner_name,
        feature      = summary.get("feature") or _pack.signoff.programme or "Change",
        run_id       = summary.get("cert_run_id") or f"CERT-{change_id[:8].upper()}",
        executed_at  = executed_at,
        role         = summary.get("role"),
        test_data    = summary.get("test_data") or {},
        cases        = summary.get("cases") or [],
        summary      = summary,
    )
    # urlquote, like the ~10 sibling download endpoints in changes.py and
    # design.py. `authority_change_id` is A2A-supplied, so it reached this
    # header unescaped — the one download route building a filename from
    # remote input without quoting it. h11 rejects an embedded CR/LF before it
    # becomes a response-splitting primitive, but the HTTP library being the
    # only thing between remote input and a header value is not a control this
    # file should rely on.
    # TRADEMARKS.md states that "certification artefacts generated by this
    # platform default to a neutral `Certification` filename prefix,
    # deliberately: a fork must not emit files named after someone else's
    # organisation." That was not true of this route — the prefix was the
    # literal "NPCI_Certification_Signoff_", so every downloaded sign-off
    # carried a third party's name in its filename regardless of deployment.
    # The frontend already honoured it via `cert.filePrefix`; only the backend
    # route did not.
    # Sanitising lives on Signoff.document_prefix() so the three routes that
    # name a generated document (here, changes.py's docx download, and the
    # cert_completion_signoff handler that stores the default name) cannot
    # disagree about it.
    filename = urlquote(
        f"{_pack.signoff.document_prefix()}_Signoff_"
        f"{change.authority_change_id or change_id}.pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ReadinessRequest(BaseModel):
    """Test data the partner ships with their readiness declaration so
    the authority's cert orchestrator can pre-configure cert-agent test cases.

    `role` selects which subset of test cases (by tc_id prefix
    PR_/PE_/RE_/BE_) gets this partner's data injected. `test_data` is a
    flat dict of role-relevant fields; only keys relevant to the role
    are read by the authority's orchestrator (payer_vpa for PAYER_PSP,
    payee_vpa for PAYEE_PSP, account_number/ifsc for institution roles).

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

    # ...and check the authority actually RECEIVED them (F-29). A ProgressReport
    # row is written whether or not its milestone_update reached the authority —
    # `report_progress` reports a delivery failure by returning None, and the
    # send is then queued in OutboundA2ARetry. Gating on the local rows alone
    # therefore let readiness be declared on milestones the authority never got,
    # leaving us "ready" against an assignment it had recorded no progress on.
    #
    # Gate on the retry queue rather than a `delivered` flag on ProgressReport:
    # the queue is the authoritative record and it self-corrects — once the
    # sweeper delivers a row it flips to 'delivered' and this check passes with
    # no reconciliation step of our own to go stale.
    undelivered = db.scalars(
        select(OutboundA2ARetry).where(
            OutboundA2ARetry.change_id == change.authority_change_id,
            OutboundA2ARetry.task_type == "milestone_update",
            OutboundA2ARetry.status.in_(("pending", "abandoned")),
        )
    ).all()
    if undelivered:
        # 'abandoned' will never self-heal — the sweeper exhausted its attempts,
        # so an operator has to re-drive those milestones. 'pending' still may.
        # Distinguish them: the caller's next move differs.
        steps = sorted({(r.payload or {}).get("milestone", "?") for r in undelivered})
        abandoned = [r for r in undelivered if r.status == "abandoned"]
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(undelivered)} milestone update(s) have not reached the authority "
                f"({', '.join(steps)}); "
                + (
                    f"{len(abandoned)} were ABANDONED after exhausting retries and must be "
                    "re-sent before readiness can be declared."
                    if abandoned else
                    "they are still queued for retry — try again once they deliver."
                )
            ),
        )

    role      = (body.role if body else None) or ""
    test_data = (body.test_data if body else None) or {}

    result = declare_ready(db, change.authority_change_id, role=role, test_data=test_data)
    if result is None:
        # The declaration itself is what unlocks certification on the authority
        # side. Setting status='ready' on an undelivered declaration reproduces
        # the exact defect this endpoint was just hardened against, one level up.
        raise HTTPException(
            status_code=502,
            detail="Readiness declaration could not be delivered to the authority; "
                   "it has been queued for retry. Status left unchanged.",
        )
    change.status = "ready"
    db.commit()

    logger.info(
        "Readiness declared: change=%s role=%s test_data_keys=%s",
        change_id, role, list(test_data.keys()),
    )
    return {"declared": True, "sent_to_npci": result is not None, "role": role}


# ── Per-Case Test Data ────────────────────────────────────────────────────────
#
# Discovery: parse the cert_test_cases ChangeDocument to enumerate TC IDs + their
# scenarios. Authority-initiated TCs are what need partner-side test data;
# partner-initiated TCs drive themselves on the partner's own simulator so
# they're elided from the form.
#
# TWO source shapes, because the authority emits two. The original is a
# `| TC ID | Scenario | Expected |` table. The workbook renderer now emits a
# HEADING document instead — `## <Actor> (C1)` sheets containing
# `### TC_1 — Success` cases. A partner that only understood the table saw
# ZERO cases in a 24-case pack and shipped an empty `test_data_per_case` with
# its readiness declaration, with no error anywhere: `authority_total: 0` and
# `discovered_from_md: true` (the doc existed; nothing in it parsed).

# Mirrors `_CASE_HEADING` in the authority's app/services/cert_catalogue.py so
# both sides agree on what counts as a case. Em-dash is what the renderer
# emits; a plain hyphen is accepted for hand-edited docs.
_CASE_HEADING_RE = re.compile(
    r"^###\s+([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9]+)\s*[—-]+\s*(\w+)\s*$",
    re.MULTILINE)

# "## Operator (C3)" — the per-actor sheet heading the workbook groups cases
# under. Mirrors `_SHEET_HEADING` in the authority's cert_catalogue.
_SHEET_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _parse_cert_test_cases_headings(content: str) -> list[dict]:
    """Pull TC rows out of the heading-shaped cert workbook.

    Deliberately does NOT re-derive the two things the authority owns:

    * `expected` is the outcome word the document publishes ("Success" /
      "Failure"), not a response code. The authority maps outcome -> expected
      code in `cert_catalogue` using the domain pack's error-code pattern; this
      side has no such pattern, and guessing one with a regex like `[A-Z]\\d+`
      would create a second source of truth that drifts from the verdict the
      authority actually asserts. The partner's use for this field is to label
      a test-data form, which the published word does correctly.
    * `api` is left empty. The authority resolves it by matching the case block
      against the ACTIVE API REGISTRY, which is authority-side state the
      partner does not hold. This is I-8's rule already stated for the table
      parser: emit nothing rather than guess a flow out of scenario prose,
      because a handler wired to a guessed flow originates the wrong call and
      reports it as the right one.

    `initiated_by` is likewise left "" (the merged view defaults it to
    authority-initiated). Sheet-to-role resolution needs the pack's role
    labels/prefixes; under a pack that declares none, the authority's own
    `_initiator_for` falls through to authority-initiated for every case, so
    "" agrees with it rather than second-guessing it.
    """
    text = content or ""
    matches = list(_CASE_HEADING_RE.finditer(text))
    # Sheet boundaries, so each case knows which actor's sheet it sits under.
    # `tc_id` is UNIQUE ONLY WITHIN A SHEET: the real 24-case pack for change
    # 8ebc2b48 numbers TC_1..TC_8 three times over, once per actor. The
    # authority never trips on this because `case_catalogue_for_change` scopes
    # to one role's sheet before it uses an id; a partner that flattens all
    # three sheets gets three cases claiming the same id. Carry the sheet so
    # callers can tell them apart instead of silently collapsing them.
    sheets = [(s.start(), s.group(1).strip())
              for s in _SHEET_HEADING_RE.finditer(text)]

    def _sheet_at(pos: int) -> str:
        return next((t for start, t in reversed(sheets) if start < pos), "")

    rows: list[dict] = []
    for i, m in enumerate(matches):
        block = text[m.end(): matches[i + 1].start()
                     if i + 1 < len(matches) else len(text)]
        rows.append({
            "tc_id":        m.group(1),
            "scenario":     _first_prose(block),
            "expected":     m.group(2).strip(),
            "initiated_by": "",
            "api":          "",
            "sheet":        _sheet_at(m.start()),
        })
    return rows


def _first_prose(block: str) -> str:
    """The case's human summary: the fenced **DETAILS** blurb when present,
    else the first non-empty, non-markup line. Bounded so one malformed case
    cannot push a multi-kilobyte block into the form."""
    lines = block.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().upper().strip("*") == "DETAILS":
            fenced: list[str] = []
            inside = False
            for nxt in lines[i + 1:]:
                if nxt.strip().startswith("```"):
                    if inside:
                        break
                    inside = True
                    continue
                if inside:
                    fenced.append(nxt.strip())
            if fenced:
                return " ".join(x for x in fenced if x)[:1000]
            break
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith(("#", "*", "`", "|", "-")):
            return s[:1000]
    return ""


def _parse_cert_test_cases_md(content: str) -> list[dict]:
    """Pull TC rows out of a cert_test_cases document.

    Returns a list of `{tc_id, scenario, expected, initiated_by, api}` dicts.
    Tries the `| TC ID | ... |` table first (the shape the authority's cert-push
    endpoint parses), then falls back to the heading shape the workbook renderer
    emits (the shape `cert_catalogue` parses). Table first, so a document that
    carries both is read the way it always was.
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
        # such column, and both this parser and the authority's cert-push one
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
    # No table in the document -> try the heading shape before giving up. Only
    # when the table parse found NOTHING: a document carrying both is read the
    # way it always was, and a partially-parsed table is not silently topped up
    # from a second parser with different field semantics.
    if not rows:
        return _parse_cert_test_cases_headings(content)
    return rows


def case_flows_for_change(db: Session, change_id: str) -> dict[str, str]:
    """`test_case_id -> outbound flow` for the change's BANK-initiated cases.

    The DB half of I-8's `case_flow_map`, which is deliberately pure (it takes
    rows, not a session), so the ChangeDocument lookup lives here beside the
    parser whose output shape it adapts.

    Returns `{}` when the suite names no partner-initiated case, or names some but
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
    informed by anything partner-specific the change docs leak."""
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
    already-saved per-TC test data the partner entered.

    Partner-initiated TCs are returned too (with `initiated_by:'BANK'`) so
    the UI can show the full picture, but the form only nudges the user
    to fill the authority-initiated rows.
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
    # (handles the case where the partner entered a TC id by hand).
    merged: list[dict] = []
    discovered_ids = set()
    for d in discovered:
        discovered_ids.add(d["tc_id"])
        row = saved.get(d["tc_id"])
        merged.append({
            "tc_id":        d["tc_id"],
            "sheet":        d.get("sheet", ""),
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
            "sheet":        "",
            "scenario":     "",
            "expected":     "",
            "initiated_by": "NPCI",
            "test_data":    row.test_data,
            "ai_suggested": bool(row.ai_suggested),
            "updated_at":   row.updated_at.isoformat(),
        })

    authority_cases = [c for c in merged if c["initiated_by"] != "BANK"]
    bank_cases = [c for c in merged if c["initiated_by"] == "BANK"]
    return {
        "change_id":          change_id,
        "cases":              merged,
        "authority_total":         len(authority_cases),
        "bank_total":         len(bank_cases),
        "authority_with_data":     sum(1 for c in authority_cases if c["test_data"]),
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
    """Save per-TC test data the partner operator just edited.

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

    api_key = resolve_runtime_api_key(db)

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
