# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Inbound A2A handler: `cert_test_response` — the per-cert-run summary NPCI
publishes back after the cert orchestrator completes. Persisted to
`incoming_changes.cert_summary`; advances cert_status out of
`ready_for_certification` once the run finishes."""
from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import IncomingChange

from ._types import TaskReceiveRequest

logger = logging.getLogger(__name__)


def handle_cert_test_response(body: TaskReceiveRequest, db: Session) -> dict:
    """Persist the per-cert-run summary NPCI publishes back after the
    cert orchestrator completes. The partner UI reads this from the
    `cert_summary` column on `incoming_changes` to render the
    pass/fail breakdown on the Certified stage.

    Wire payload shape (from app.services.cert_orchestrator):
      { cert_run_id, external_run_id, feature_name, flow, bank_id,
        role, total, passed, failed, skipped, completed_at,
        results: [{tc_id, status, expected_code, actual_code, …}] }
    """
    payload = body.payload or {}
    npci_change_id = body.change_id or payload.get("change_id") or ""

    # The wire's change_id is NPCI's; map to the local row.
    change = (
        db.query(IncomingChange)
          .filter(IncomingChange.npci_change_id == npci_change_id)
          .first()
    )
    if not change:
        logger.warning(
            "cert_test_response for unknown change npci_change_id=%s — accepting but no-op",
            npci_change_id,
        )
        return {"status": "accepted", "message": "Unknown change — discarded"}

    # Normalise shape — partner UI expects {total,passed,failed,cases}.
    # IMPORTANT: cert lifecycle ships TWO messages on the same wire — first
    # the per-TC results, then a signoff payload that overwrites the totals
    # but DOES NOT carry the `results` list. Preserve any previously-stored
    # `cases` when the inbound payload has no per-TC detail, so the signoff
    # doesn't clobber the results table the partner is rendering.
    new_results = payload.get("results") or []
    existing_cases: list = []
    if change.cert_summary and not new_results:
        try:
            existing_cases = (_json.loads(change.cert_summary) or {}).get("cases", []) or []
        except Exception:
            existing_cases = []
    summary = {
        "cert_run_id":     payload.get("cert_run_id"),
        "external_run_id": payload.get("external_run_id"),
        "feature_name":    payload.get("feature_name"),
        "flow":            payload.get("flow"),
        "role":            payload.get("role"),
        "total":           payload.get("total", 0),
        "passed":          payload.get("passed", 0),
        "failed":          payload.get("failed", 0),
        "skipped":         payload.get("skipped", 0),
        "completed_at":    payload.get("completed_at"),
        # Wire field is `results`; UI reads `cases` for clarity.
        "cases":           new_results or existing_cases,
        # Slice 4/5 — phase split + signoff metadata pass through whenever
        # the wire includes them so the partner UI can show 7 NPCI / 7 BANK
        # tags and the certificate validity period.
        "phases":          payload.get("phases"),
        "valid_until":     payload.get("valid_until"),
        "signoff_message": payload.get("signoff_message"),
    }
    change.cert_summary = _json.dumps(summary)
    # Advance cert_status out of `ready_for_certification` once the run
    # finishes — otherwise the partner UI sits on "NPCI Orchestration"
    # forever. Distinguish all-PASS (terminal success) from a completed
    # run with failures (terminal but needs review on the partner side).
    total = (summary["passed"] or 0) + (summary["failed"] or 0)
    new_cert_status = None
    if summary["passed"] > 0 and summary["failed"] == 0:
        new_cert_status = "certified"
    elif total > 0:
        new_cert_status = "tests_completed"

    if new_cert_status:
        history = {}
        if change.cert_status_history:
            try:
                history = _json.loads(change.cert_status_history) or {}
            except Exception:
                history = {}
        history[new_cert_status] = datetime.now(timezone.utc).isoformat()
        change.cert_status = new_cert_status
        change.cert_status_history = _json.dumps(history)
    db.commit()
    logger.info(
        "cert_test_response persisted: change=%s run=%s passed=%s failed=%s",
        change.id, summary["cert_run_id"], summary["passed"], summary["failed"],
    )
    return {
        "status": "accepted",
        "message": "Cert results recorded",
        "cert_run_id": summary["cert_run_id"],
        "passed":      summary["passed"],
        "failed":      summary["failed"],
    }
