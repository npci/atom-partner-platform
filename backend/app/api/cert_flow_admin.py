"""Manual overrides for the A2A cert lifecycle — bank-side actions.

Today ``handle_cert_config_request`` (``a2a_common/handlers/cert_lifecycle.py``)
auto-replies with ``cert_config_submission`` on the same A2A task, so a
successful round-trip needs no operator involvement. This module adds a
one-off manual send path for the case where the operator wants to re-fire
the submission against a specific ``change_id`` without waiting for NPCI to
send a new cert_config_request — e.g. after fixing an api-sim validation
issue that rejected the first attempt.

Every route requires a logged-in session (cookie or Bearer, same as the rest of
the partner API): each of these sends to NPCI authenticated as the bank. The
Bank Sim SPA at ``/a2a-banksim/`` has no login, so its buttons get a 401 here.

Calls the existing ``npci_client.send_cert_config_submission`` helper
(which itself uses ``send_task``), so wire format + audit are identical
to the auto-reply.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import os, json, ssl
import urllib.request
import urllib.parse

from app.api.auth import get_current_user
from app.database import get_db
from app.a2a_common.handlers.cert_lifecycle import handle_cert_config_request
from app.npci_client import (
    send_cert_config_submission,
    send_cert_test_preparation,
    send_cert_case_result,
    send_cert_waiver_request,
    send_cert_verdict_dispute,
    send_cert_verdict_acceptance,
    send_cert_fix_notification,
    send_cert_val_request,
    send_cert_completion_check_request,
    send_cert_signoff_request,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cert-flow", tags=["cert-flow"],
                   dependencies=[Depends(get_current_user)])


class ResendConfigSubmissionRequest(BaseModel):
    change_id: str
    # Optional overrides on top of the demo profile the auto-reply builds.
    # Keys here are the flat extension keys ``bank_org_id``, ``bank_code``,
    # etc. — same vocabulary the auto-reply's ``config`` dict carries. Any
    # unspecified key is filled from the demo profile.
    config_overrides: dict | None = None


@router.post("/send-config-submission")
def resend_config_submission(
    body: ResendConfigSubmissionRequest,
    db: Session = Depends(get_db),
):
    """Fire a fresh cert_config_submission for a given change_id.

    Composes the payload the same way ``handle_cert_config_request`` does
    (so NPCI sees a byte-identical envelope), applies any ``config_overrides``,
    and sends via the existing ``send_cert_config_submission`` helper.
    """
    # Build the auto-reply body by calling the handler with a synthetic
    # inbound envelope. This keeps the demo profile as the single source of
    # truth — a copy of the config would silently drift.
    class _StubBody:
        payload = {"change_id": body.change_id}

    auto_reply = handle_cert_config_request(_StubBody(), db)
    config = dict(auto_reply.get("config") or {})

    if body.config_overrides:
        config.update(body.config_overrides)

    result = send_cert_config_submission(db, body.change_id, config)
    if result is None:
        raise HTTPException(
            status_code=502,
            detail="send_cert_config_submission returned None — see partner_backend logs",
        )
    logger.info(
        "resend_config_submission: change=%s bank_org_id=%s psp_org_id=%s → %s",
        body.change_id, config.get("bank_org_id"), config.get("psp_org_id"),
        result.get("message_id") or result,
    )
    return {
        "status": "sent",
        "change_id": body.change_id,
        "message_id": result.get("message_id"),
        "task_state": result.get("task_state"),
        "config_summary": {
            "bank_org_id": config.get("bank_org_id"),
            "psp_org_id":  config.get("psp_org_id"),
        },
    }


class SendTestPreparationRequest(BaseModel):
    change_id: str
    subset: str
    # Optional case-id filter. Empty/None ⇒ send the whole map for the subset.
    # The A2A spec (§4) marks cert_test_preparation as "Incremental" — the
    # bank declares per-case readiness in as many messages as it wants, not
    # necessarily all at once. This endpoint lets the operator drive that
    # cadence from the Bank Sim console.
    case_ids: list[str] | None = None
    cflow_id: str | None = None


_APISIM_URL = os.environ.get("PARTNER_APISIM_URL", "https://host.docker.internal:8094")


def _fetch_partner_testdata(subset: str) -> dict:
    """Mirror of the reader in cert_lifecycle.py — read testdata from api-sim.

    Duplicated here rather than imported to keep this module's dependency
    surface minimal (one urllib + one env var); the map shape is identical.
    """
    if not subset:
        return {}
    url = f"{_APISIM_URL.rstrip('/')}/internal/subset/{subset}/testdata"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(url, context=ctx, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else {}


@router.post("/send-test-preparation")
def send_test_preparation(
    body: SendTestPreparationRequest,
    db: Session = Depends(get_db),
):
    """Fire an incremental cert_test_preparation for a subset (or a slice of it).

    A2A spec §4 cert_test_preparation is Bank→NPCI, Incremental — a fresh
    outbound send per operator click, carrying the case_data map for the
    named cases. All-cases mode when case_ids is empty.
    """
    if not body.subset:
        raise HTTPException(status_code=400, detail="subset is required")
    try:
        full_map = _fetch_partner_testdata(body.subset)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"api-sim unavailable: {e}")

    if body.case_ids:
        picked = [cid for cid in body.case_ids if cid in full_map]
        case_data = {cid: full_map[cid] for cid in picked}
        missing = [cid for cid in body.case_ids if cid not in full_map]
    else:
        case_data = dict(full_map)
        missing = []

    if not case_data:
        raise HTTPException(
            status_code=404,
            detail=f"No partner testdata rows for subset={body.subset!r}"
                   + (f", requested case_ids={body.case_ids}" if body.case_ids else ""),
        )

    # Bank identity to authenticate AS — picks the right api_key from
    # partner_settings.partner_api_key_by_org when set. Falls back to the
    # global partner_api_key when the map has no entry for this bank.
    from app.adapters import get_switch_adapter
    bank_org_id = get_switch_adapter().bank_org_id()
    result = send_cert_test_preparation(db, body.change_id, case_data, bank_org_id=bank_org_id)
    logger.info(
        "cert_test_preparation sent: change=%s subset=%s case_count=%d missing=%s",
        body.change_id, body.subset, len(case_data), missing,
    )
    return {
        "status": "sent",
        "change_id": body.change_id,
        "subset": body.subset,
        "case_count": len(case_data),
        "case_ids": list(case_data.keys()),
        "missing_case_ids": missing,
        "message_id": (result or {}).get("message_id"),
    }


class SendCaseResultRequest(BaseModel):
    change_id: str
    case_id: str
    status: str          # spec: passed | failed | skipped | error | waived
    attempt: int = 1
    details: dict | None = None
    cflow_id: str | None = None


@router.post("/send-case-result")
def send_case_result(
    body: SendCaseResultRequest,
    db: Session = Depends(get_db),
):
    """Fire cert_case_result (Bank→NPCI) for a single (case_id, attempt).

    A2A spec §4 cert_case_result direction is "Either", cardinality
    "per (case_id, attempt)". This endpoint is the Bank-initiated leg;
    NPCI-initiated goes through cert_a2a_trigger on the NPCI side.
    """
    from app.adapters import get_switch_adapter
    bank_org_id = get_switch_adapter().bank_org_id()
    result = send_cert_case_result(
        db, body.change_id, body.case_id, body.status, body.attempt,
        details=body.details, bank_org_id=bank_org_id,
    )
    if result is None:
        raise HTTPException(status_code=502, detail="send_cert_case_result returned None")
    logger.info(
        "cert_case_result sent: change=%s case=%s attempt=%d status=%s",
        body.change_id, body.case_id, body.attempt, body.status,
    )
    return {
        "status": "sent",
        "change_id": body.change_id,
        "case_id": body.case_id,
        "attempt": body.attempt,
        "case_status": body.status,
        "message_id": (result or {}).get("message_id"),
    }


class SendWaiverRequestBody(BaseModel):
    change_id: str
    case_id: str
    reason: str = ""
    # Spec §7.8 categorises the ask: "cannot_execute" (bank lacks capability),
    # "environment_unavailable" (test infra down), "not_applicable" (case
    # doesn't apply to this bank), "other".
    category: str = "cannot_execute"
    cflow_id: str | None = None


@router.post("/send-waiver-request")
def send_waiver_request(
    body: SendWaiverRequestBody,
    db: Session = Depends(get_db),
):
    """Fire cert_waiver_request (Bank→NPCI) for a specific case.

    A2A spec §4 cert_waiver_request: Bank asks NPCI to accept the cert
    without running this case (e.g., partner doesn't support the capability
    or the test infra is unreachable). NPCI responds with cert_waiver_decision
    (granted / denied). Requesting a waiver DOES NOT block the case from
    being reported later — the bank can still send cert_case_result if it
    changes its mind.
    """
    from app.adapters import get_switch_adapter
    bank_org_id = get_switch_adapter().bank_org_id()
    result = send_cert_waiver_request(
        db, body.change_id, body.case_id,
        category=body.category or "cannot_execute",
        reason=body.reason or "",
        bank_org_id=bank_org_id,
    )
    if result is None:
        raise HTTPException(status_code=502, detail="send_cert_waiver_request returned None")
    logger.info(
        "cert_waiver_request sent: change=%s case=%s category=%s",
        body.change_id, body.case_id, body.category,
    )
    return {
        "status": "sent",
        "change_id": body.change_id,
        "case_id":   body.case_id,
        "category":  body.category,
        "reason":    body.reason,
        "message_id": (result or {}).get("message_id"),
    }


class SendCertTaskBody(BaseModel):
    change_id: str
    task_type: str
    payload: dict = {}


_CERT_TASK_SENDERS = {
    "cert_verdict_dispute":          (("case_id",), lambda db, c, p: send_cert_verdict_dispute(
        db, c, p["case_id"], p.get("bank_position", ""), p.get("requested_action", "re_triage"))),
    "cert_verdict_acceptance":       (("case_id",), lambda db, c, p: send_cert_verdict_acceptance(db, c, p["case_id"])),
    "cert_fix_notification":         (("fixed_case_ids",), lambda db, c, p: send_cert_fix_notification(
        db, c, p["fixed_case_ids"], p.get("fix_summary", ""), p.get("ready_for_rerun", True), p.get("case_data"))),
    "cert_val_request":          (("batch_id",), lambda db, c, p: send_cert_val_request(
        db, c, p["batch_id"], p.get("case_ids"))),
    "cert_completion_check_request": (("cflow_id",), lambda db, c, p: send_cert_completion_check_request(db, c, p["cflow_id"])),
    "cert_signoff_request":          (("cflow_id",), lambda db, c, p: send_cert_signoff_request(db, c, p["cflow_id"])),
}


@router.post("/send-cert-task")
def send_cert_task(body: SendCertTaskBody, db: Session = Depends(get_db)):
    """Fire a bank-started certification task at NPCI: dispute, acceptance, fix
    (with refreshed case_data), results pull, completion check or sign-off request."""
    entry = _CERT_TASK_SENDERS.get(body.task_type)
    if entry is None:
        raise HTTPException(status_code=400, detail=f"unsupported task_type; expected one of {sorted(_CERT_TASK_SENDERS)}")
    required, send = entry
    missing = [k for k in required if not body.payload.get(k)]
    if missing:
        raise HTTPException(status_code=400, detail=f"{body.task_type} needs {', '.join(missing)} in payload")
    result = send(db, body.change_id, body.payload)
    if result is None:
        raise HTTPException(status_code=502, detail=f"{body.task_type} send failed")
    return {"status": "sent", "task_type": body.task_type, "change_id": body.change_id,
            "message_id": (result or {}).get("message_id")}


_NPCI_INTERNAL_URL = os.environ.get(
    "NPCI_INTERNAL_URL", "http://npci_backend:8000")


@router.get("/a2a-messages")
def list_a2a_messages(
    limit: int = 50,
    change_id: str | None = None,
    cflow_id: str | None = None,
):
    """Recent A2A conversation for THIS bank — reverse-chronological.

    The partner side has no local a2a_messages table (its inbound SDK
    handlers ack without persisting), so we proxy-call NPCI's
    ``/internal/cert-a2a/messages`` endpoint filtered to Bank Sim's
    partner_id. Same origin from the bank-sim frontend's perspective
    (nginx proxies /a2a-banksim/a2a/* → partner_backend), while the
    partner_backend → npci_backend hop uses the docker network alias.
    """
    # Bank Sim's registered partner_id on NPCI's side. Hardcoded because
    # partner_backend represents exactly one bank identity per deployment
    # (see the switch adapter's bank_org_id()); the operator can override via
    # env if they run this against a different NPCI stack.
    bank_partner_id = os.environ.get(
        "NPCI_PARTNER_ID", "44444444-4444-4444-8444-444444444444")

    params: dict = {"partner_id": bank_partner_id, "limit": limit}
    if change_id: params["change_id"] = change_id
    if cflow_id:  params["cflow_id"] = cflow_id

    url = f"{_NPCI_INTERNAL_URL.rstrip('/')}/api/internal/cert-a2a/messages"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                url + "?" + urllib.parse.urlencode(params),
                headers={"Accept": "application/json"},
            ),
            timeout=10,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502,
            detail=f"could not fetch A2A messages from NPCI: {e}")
