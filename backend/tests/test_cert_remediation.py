# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CERT-5 partner half: fix rounds, findings conversion, and the approval gate.

The two load-bearing properties: five failing cases make ONE fix job
(`open_round` appends), and `send_cert_fix_notification` is reachable from
exactly one place — the approval endpoint — which refuses rounds that are not
ready and leaves a round PARKED when the send fails.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.npci_client as npci_client
from app.api.dashboard.cert_fix import approve_and_notify, mark_round_fixed
from app.models import CertFixRound, IncomingChange
from app.services.cert_remediation import (
    open_round, run_fix_round, verdicts_to_findings,
)

USER = SimpleNamespace(username="operator-1", id="u1")
# Fix rounds and the approval route are keyed on the LOCAL id; the wire only
# understands the authority's. DELIBERATELY DIFFERENT VALUES — equal ids would
# let the endpoint send either one and still look correct.
CHANGE = "chg-1"
NPCI_CHANGE = "npci-chg-1"


@pytest.fixture(autouse=True)
def _change_row(db_session):
    """The approval endpoint resolves `IncomingChange` to translate the local
    id to the authority's, so the row has to exist."""
    db_session.add(IncomingChange(id=CHANGE, npci_change_id=NPCI_CHANGE,
                                  title="A change"))
    db_session.commit()


def _verdict(tc="TC1", failures=None):
    return {"test_case_id": tc, "classification": "real_defect",
            "assertion_failures": failures if failures is not None else [
                {"field": "ReqPay/Amt/@value", "kind": "length",
                 "expected": {"length_rule": "Max Length 10"}, "reason": "over"}]}


# ── open_round: one round per verdict batch ──────────────────────────────────

def test_five_failures_produce_one_fix_round(db_session):
    for i in range(5):
        rnd = open_round(db_session, change_id="chg-1", cflow_id="CF-1",
                         case_id=f"TC{i}", verdict=_verdict(f"TC{i}"))
    rounds = db_session.query(CertFixRound).all()
    assert len(rounds) == 1
    assert sorted(rnd.verdict_case_ids) == [f"TC{i}" for i in range(5)]
    assert len(rnd.verdicts) == 5


def test_duplicate_case_is_not_appended_twice(db_session):
    open_round(db_session, change_id="chg-1", cflow_id=None, case_id="TC1",
               verdict=_verdict())
    rnd = open_round(db_session, change_id="chg-1", cflow_id=None, case_id="TC1",
                     verdict=_verdict())
    assert rnd.verdict_case_ids == ["TC1"]


def test_a_closed_round_opens_a_successor_with_the_next_number(db_session):
    first = open_round(db_session, change_id="chg-1", cflow_id=None,
                       case_id="TC1", verdict=_verdict())
    first.status = "approved"
    db_session.commit()
    second = open_round(db_session, change_id="chg-1", cflow_id=None,
                        case_id="TC2", verdict=_verdict("TC2"))
    assert second.id != first.id
    assert second.round_number == 2


# ── verdicts_to_findings: pure, file stays empty ─────────────────────────────

def test_findings_are_pure_and_leave_file_empty():
    """A cert verdict names an API and an xpath, never a source file —
    guessing a path would aim the fix agent at the wrong code."""
    findings = verdicts_to_findings([_verdict()])
    assert len(findings) == 1
    assert findings[0]["file"] == ""
    assert "ReqPay/Amt/@value" in findings[0]["title"]
    assert "length" in findings[0]["description"]


def test_one_finding_per_assertion_failure():
    verdict = _verdict(failures=[
        {"field": "A", "kind": "enum", "expected": {}, "reason": "r1"},
        {"field": "B", "kind": "pattern", "expected": {}, "reason": "r2"},
    ])
    assert len(verdicts_to_findings([verdict])) == 2


def test_code_only_failure_still_yields_a_finding():
    findings = verdicts_to_findings([{"test_case_id": "TC1",
                                      "expected_code": "00", "actual_code": "ZM",
                                      "assertion_failures": []}])
    assert len(findings) == 1 and findings[0]["file"] == ""


# ── the fix worker parks honestly ────────────────────────────────────────────

def test_unmappable_findings_park_at_awaiting_manual_fix(db_session):
    rnd = open_round(db_session, change_id="chg-1", cflow_id=None,
                     case_id="TC1", verdict=_verdict())
    run_fix_round(rnd.id)
    db_session.expire_all()
    rnd = db_session.get(CertFixRound, rnd.id)
    assert rnd.status == "awaiting_manual_fix"
    assert "manually" in (rnd.fix_note or "").lower() or "Fix manually" in rnd.fix_note


# ── the approval gate ────────────────────────────────────────────────────────

def _sent(monkeypatch, reply):
    calls = []

    def fake(db, change_id, fixed_case_ids, fix_summary="", ready_for_rerun=True):
        calls.append({"change_id": change_id, "fixed_case_ids": fixed_case_ids})
        return reply

    monkeypatch.setattr(npci_client, "send_cert_fix_notification", fake)
    return calls


def test_approval_refuses_a_round_not_awaiting_approval(db_session, monkeypatch):
    calls = _sent(monkeypatch, {"status": "delivered"})
    rnd = open_round(db_session, change_id="chg-1", cflow_id=None,
                     case_id="TC1", verdict=_verdict())
    with pytest.raises(HTTPException) as exc:
        approve_and_notify("chg-1", rnd.id, user=USER, db=db_session)
    assert exc.value.status_code == 409
    assert calls == [], "the notification must not fire for an unready round"


def test_mark_fixed_then_approve_sends_and_records(db_session, monkeypatch):
    calls = _sent(monkeypatch, {"status": "delivered"})
    rnd = open_round(db_session, change_id="chg-1", cflow_id=None,
                     case_id="TC1", verdict=_verdict())
    mark_round_fixed("chg-1", rnd.id, user=USER, db=db_session)
    out = approve_and_notify("chg-1", rnd.id, user=USER, db=db_session)
    assert out["status"] == "approved"
    assert out["approved_by"] == "operator-1"
    assert calls and calls[0]["fixed_case_ids"] == ["TC1"]
    assert calls[0]["change_id"] == NPCI_CHANGE, \
        "the notification goes on the wire — the authority's id, not the local one"


def test_failed_send_leaves_the_round_parked_not_approved(db_session, monkeypatch):
    """Telling the bank the authority was notified when it was not is the
    worse failure — a send that returns None (npci_client's failure shape)
    keeps the round at awaiting_approval."""
    _sent(monkeypatch, None)
    rnd = open_round(db_session, change_id="chg-1", cflow_id=None,
                     case_id="TC1", verdict=_verdict())
    mark_round_fixed("chg-1", rnd.id, user=USER, db=db_session)
    with pytest.raises(HTTPException) as exc:
        approve_and_notify("chg-1", rnd.id, user=USER, db=db_session)
    assert exc.value.status_code == 502
    db_session.expire_all()
    assert db_session.get(CertFixRound, rnd.id).status == "awaiting_approval"


def test_approval_endpoint_is_the_only_caller_of_the_notification():
    """Source-level pin: `send_cert_fix_notification` has exactly one call
    site outside npci_client itself — the approval endpoint."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    callers = []
    for py in root.rglob("*.py"):
        if py.name == "npci_client.py":
            continue
        text = py.read_text()
        if "send_cert_fix_notification(" in text:
            callers.append(py.name)
    assert callers == ["cert_fix.py"], f"unexpected callers: {callers}"


# ── the verdict handler branch ───────────────────────────────────────────────

def test_real_defect_opens_a_round_and_acks_not_waives(db_session):
    from app.a2a_common.handlers._types import TaskReceiveRequest
    from app.a2a_common.handlers.cert_lifecycle import handle_cert_verdict_notification

    body = TaskReceiveRequest(task_type="cert_verdict_notification",
                              change_id="chg-1", payload=_verdict())
    reply = handle_cert_verdict_notification(body, db_session)
    assert reply["task_type"] == "cert_defect_ack"
    assert db_session.query(CertFixRound).count() == 1


def test_waiver_eligible_keeps_the_existing_reply(db_session):
    from app.a2a_common.handlers._types import TaskReceiveRequest
    from app.a2a_common.handlers.cert_lifecycle import handle_cert_verdict_notification

    body = TaskReceiveRequest(
        task_type="cert_verdict_notification", change_id="chg-1",
        payload={"test_case_id": "TC1", "classification": "waiver_eligible"})
    reply = handle_cert_verdict_notification(body, db_session)
    assert reply["task_type"] == "cert_waiver_request"
    assert db_session.query(CertFixRound).count() == 0
