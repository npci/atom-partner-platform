"""Partner-side cert lifecycle handlers — the bank's half of the A2A cert conversation.

NPCI (the cert engine) drives the conversation; these produce the bank's replies:
  cert_config_request      -> cert_config_submission  (the bank's config)
  cert_setup_notification  -> cert_test_preparation   (per-case data + readiness)
  cert_verdict_notification-> cert_waiver_request      (on a waiver-eligible failure)
cert_case_result / cert_waiver_decision / cert_signoff_notification are acknowledged; their
persistence rides the existing cert_test_response handler.

These four are dispatched from `partner_executor.build_handler_registry()`. They were
unwired between 68cd0a44 and this session — see the regression note in that module.

This module owns the protocol — message shapes, verdict triage, the identity echo. What
is specific to a bank (who it is, where its switch listens, per-case test data) comes
from the configured `SwitchAdapter` (`app.adapters`); the stock `static` adapter is the
demo profile that used to live here.
"""
from __future__ import annotations

import logging

from app.adapters import get_switch_adapter

logger = logging.getLogger(__name__)


def handle_cert_config_request(body, db) -> dict:
    """Reply to CERT_CONFIG_REQUEST with the bank's certification config."""
    p = getattr(body, "payload", None) or {}
    # The subset is the bank's choice; NPCI's `scope.subset_hint` only fills in
    # when the adapter names none.
    cfg = get_switch_adapter().bank_config((p.get("scope") or {}).get("subset_hint") or "")
    subset = cfg.config.get("requested_subset") or ""
    reply = {
        "task_type": "cert_config_submission", "status": "accepted",
        "summary": (
            f"Certification configuration submitted: PSP {cfg.psp_org_id}; "
            f"switch endpoint {cfg.switch_endpoint}; requesting {subset}; "
            f"certificates provided out-of-band."
        ),
        "config": cfg.config,
    }
    # Echo the identity this bank actually is, so NPCI can refuse an off-target
    # submission by comparing it with the sub-entry it asked about.
    target = p.get("target_identity")
    if isinstance(target, dict) and target.get("org_id"):
        reply["identity"] = {
            "product": target.get("product") or "upi",
            "org_id": cfg.psp_org_id,
            "display_name": target.get("display_name"),
            "kind": target.get("kind"),
        }
    return reply


def handle_cert_setup_notification(body, db) -> dict:
    """Reply to CERT_SETUP_NOTIFICATION with per-case data for the announced scope.

    The per-case values come from the adapter, which must cover every announced
    case so the reply is always complete.
    """
    payload = getattr(body, "payload", None) or {}
    subset = payload.get("subset") or ""
    # The spec's case_list is authoritative; `cases` is the platform's flat alias.
    cases = [c.get("case_id") for c in (payload.get("case_list") or []) if c.get("case_id")]
    if not cases:
        cases = payload.get("cases", []) or []

    data = get_switch_adapter().case_data(subset, cases, payload)
    ready = sum(1 for d in data.case_data.values() if d.get("ready"))
    return {
        "task_type": "cert_test_preparation", "status": "accepted",
        "summary": (
            f"Test data prepared for {len(cases)} test case(s): virtual payment addresses, "
            f"MPIN, and transaction amounts. {ready} declared ready to execute{data.detail}."
        ),
        # `case_data` is the spec key; `test_data` is kept as an alias so an NPCI
        # side that has not picked up the rename still receives the values.
        "case_data": data.case_data,
        "test_data": data.case_data,
        **data.extras,
    }


def handle_cert_case_result(body, db) -> dict:
    """Ack a case result. For bank-initiated cases (reporter=bank) the bank
    formally reports the outcome back to NPCI — the spec makes cert_case_result
    bidirectional (Direction: Either; the bank fires for initiator=bank cases).
    This reply renders as a bank -> NPCI message in the conversation."""
    p = getattr(body, "payload", None) or {}
    if p.get("reporter") == "bank":
        tc = p.get("test_case_id") or p.get("case_id", "")
        # `status` carries the spec vocabulary (passed/failed/error) since Phase 3.
        result = p.get("status")
        return {
            "task_type": "cert_case_result", "reporter": "bank", "status": "accepted",
            "test_case_id": tc, "case_id": tc, "result": result,
            "summary": f"Bank reports test case {tc}: {result}.",
        }
    return {"status": "accepted"}  # NPCI-initiated: silent ack, no reply bubble


def build_verdict_reply(p: dict) -> dict:
    """The reply to a CERT_VERDICT_NOTIFICATION, with no side effects.

    Split from the handler so a console can preview the reply without sending a
    `cert_verdict_acceptance` to NPCI as a by-product.

    NPCI sends ONE verdict per initiator (`initiator`, `verdicts[]`); the older
    single-case shape (top-level `test_case_id` / `classification`) is still read.
    Request a waiver only for cases NPCI classified `waiver_eligible`; any other
    classification is a defect the bank owns, so it accepts those. No
    classification keeps the old behaviour (request a waiver).
    """
    entries = [v for v in (p.get("verdicts") or []) if isinstance(v, dict)]
    if not entries and (p.get("test_case_id") or p.get("case_id")):
        entries = [{"case_id": p.get("test_case_id") or p.get("case_id"),
                    "classification": p.get("classification")}]
    entries = [{**e, "case_id": e.get("case_id") or e.get("test_case_id")} for e in entries]
    waive = [e["case_id"] for e in entries
             if not e.get("classification") or e["classification"] == "waiver_eligible"]
    accept = [e["case_id"] for e in entries if e["case_id"] not in waive]
    initiator = p.get("initiator") or "npci"
    parts = []
    if waive:
        parts.append(f"waiver requested for {len(waive)} case(s) ({', '.join(waive)}): "
                     "functionality not applicable to our deployment")
    if accept:
        parts.append(f"verdict accepted for {len(accept)} case(s) ({', '.join(accept)}); "
                     "the bank will fix and retest")
    return {
        "task_type": "cert_waiver_request" if waive else "cert_verdict_acceptance",
        "status": "accepted", "initiator": initiator,
        "summary": f"{initiator.upper()}-initiated verdict: " + "; ".join(parts) + ".",
        "case_ids": [e["case_id"] for e in entries],
        # Spec `cert_waiver_request` splits these: `category` is the enum
        # (non_applicable | deferred | infeasible | policy), `reason` the prose.
        **({"category": "non_applicable",
            "reason": "Functionality is not applicable to this deployment."} if waive else {}),
        "waiver_requests": [{"case_id": c} for c in waive],
        "accepted_case_ids": accept,
    }


def handle_cert_verdict_notification(body, db) -> dict:
    """Reply to CERT_VERDICT_NOTIFICATION, and send NPCI the acceptance for any
    case the bank is not asking a waiver for (NPCI records an acceptance from the
    `cert_verdict_acceptance` task, not from this reply)."""
    p = getattr(body, "payload", None) or {}
    reply = build_verdict_reply(p)
    if reply["accepted_case_ids"]:
        from ._background import schedule_verdict_acceptance
        schedule_verdict_acceptance(
            getattr(body, "change_id", None) or p.get("change_id") or "",
            reply["accepted_case_ids"], getattr(body, "cflow_id", None))
    return reply


def handle_cert_waiver_decision(body, db) -> dict:
    """NPCI's decisions on waiver requests, ONE message for every case decided
    (`decisions[]`). A denial means the case stays failed and the run is blocked
    until the bank fixes it or disputes the verdict."""
    p = getattr(body, "payload", None) or {}
    decisions = [d for d in (p.get("decisions") or []) if isinstance(d, dict)]
    if not decisions and (p.get("test_case_id") or p.get("case_id")):
        decisions = [{"case_id": p.get("test_case_id") or p.get("case_id"),
                      "decision": p.get("decision"), "reason": p.get("reason")}]
    denied = []
    for d in decisions:
        tc = d.get("case_id") or d.get("test_case_id")
        if d.get("decision") == "denied":
            denied.append(tc)
            logger.warning("waiver DENIED: case=%s reason=%s -> fix and retest, or dispute",
                           tc, d.get("reason"))
        else:
            logger.info("waiver decision: case=%s decision=%s", tc, d.get("decision"))
    return {"status": "accepted",
            "decisions": [{"case_id": d.get("case_id") or d.get("test_case_id"),
                           "decision": d.get("decision")} for d in decisions],
            "next": "fix_or_dispute" if denied else "none"}


def handle_cert_verdict_dispute_response(body, db) -> dict:
    """NPCI's answer to a dispute, ONE message for every disputed case (`responses[]`)."""
    p = getattr(body, "payload", None) or {}
    responses = [r for r in (p.get("responses") or []) if isinstance(r, dict)]
    if not responses and (p.get("test_case_id") or p.get("case_id")):
        responses = [p]
    for r in responses:
        logger.info("dispute response: case=%s outcome=%s",
                    r.get("case_id") or r.get("test_case_id"),
                    r.get("outcome") or r.get("decision") or r.get("verdict"))
    return {"status": "accepted",
            "case_ids": [r.get("case_id") or r.get("test_case_id") for r in responses]}


def handle_cert_completion_report(body, db) -> dict:
    p = getattr(body, "payload", None) or {}
    summary = p.get("summary") if isinstance(p.get("summary"), dict) else {}
    logger.info("completion report: complete=%s passed=%s failed=%s incomplete=%s",
                summary.get("complete"), summary.get("passed"),
                summary.get("failed"), summary.get("incomplete"))
    return {"status": "accepted"}

