# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Regressions from the aggressive validation pass (defects D4 and D5).

Both were reproduced by a tester with two synchronised callers against code
whose full suite was green — the sequential tests could not see either.

  D4 (MAJOR) `open_round` was a bare select-then-insert: two verdicts for one
     batch both saw "no open round" and both created round 1, so one verdict
     batch became two jobs and remediation attribution broke.

  D5 (MAJOR) the approval endpoint read the status and THEN sent: two callers
     both observed `awaiting_approval` and both sent, so the authority
     received duplicate fix notifications and could re-run the round twice.

Threads are not needed to prove D5 — the send itself is the interleaving
point, so a stub that RE-ENTERS the endpoint mid-send reproduces the exact
window deterministically, on any database.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.npci_client as npci_client
from app.api.dashboard.cert_fix import approve_and_notify, mark_round_fixed
from app.models import CertFixRound, IncomingChange
from app.services.cert_remediation import open_round

USER = SimpleNamespace(username="operator-1", id="u1")
# Local id vs the authority's — deliberately different, see test_cert_remediation.
CHANGE = "chg-1"
NPCI_CHANGE = "npci-chg-1"


@pytest.fixture(autouse=True)
def _change_row(db_session):
    """The approval endpoint resolves `IncomingChange` to translate the local
    id to the authority's before sending."""
    db_session.add(IncomingChange(id=CHANGE, npci_change_id=NPCI_CHANGE,
                                  title="A change"))
    db_session.commit()


def _verdict(tc="TC-A"):
    return {"test_case_id": tc, "classification": "real_defect",
            "assertion_failures": [{"field": "ReqPay/Amt", "kind": "length",
                                    "expected": {}, "reason": "over"}]}


# ── D4: one batch is one round ───────────────────────────────────────────────

def test_round_number_is_unique_per_change_in_the_schema():
    """The database — not just the code path — must refuse a second round 1.
    Without this the race has nothing to lose against."""
    constraints = {
        tuple(sorted(c.columns.keys()))
        for c in CertFixRound.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert ("change_id", "round_number") in constraints, \
        "UNIQUE(change_id, round_number) is missing — concurrent verdicts can " \
        "create duplicate round 1s"


def test_duplicate_round_number_is_rejected_by_the_database(db_session):
    """THE D4 REPRO, reduced to what the race produces: two round-1 rows for
    one change. The schema must make that impossible."""
    from sqlalchemy.exc import IntegrityError

    db_session.add(CertFixRound(change_id=CHANGE, round_number=1,
                                verdict_case_ids=["TC-A"], verdicts=[]))
    db_session.commit()
    db_session.add(CertFixRound(change_id=CHANGE, round_number=1,
                                verdict_case_ids=["TC-B"], verdicts=[]))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_a_second_verdict_appends_to_the_existing_round(db_session):
    open_round(db_session, change_id=CHANGE, cflow_id="CF", case_id="TC-A",
               verdict=_verdict("TC-A"))
    rnd = open_round(db_session, change_id=CHANGE, cflow_id="CF", case_id="TC-B",
                     verdict=_verdict("TC-B"))
    assert db_session.query(CertFixRound).count() == 1
    assert sorted(rnd.verdict_case_ids) == ["TC-A", "TC-B"]


def test_open_round_retries_into_the_winner_after_an_insert_race(db_session, monkeypatch):
    """Simulate losing the insert race: the first flush raises IntegrityError
    as though another caller inserted round 1 first. The loser must recover by
    appending to the existing round, not by failing or duplicating."""
    from sqlalchemy.exc import IntegrityError

    # The 'winner' round already exists.
    db_session.add(CertFixRound(change_id=CHANGE, round_number=1,
                                status="open", verdict_case_ids=["TC-A"],
                                verdicts=[]))
    db_session.commit()

    real_flush = db_session.flush
    state = {"raised": False}

    def flaky_flush(*a, **kw):
        if not state["raised"]:
            state["raised"] = True
            raise IntegrityError("simulated race", None, Exception())
        return real_flush(*a, **kw)

    monkeypatch.setattr(db_session, "flush", flaky_flush)
    rnd = open_round(db_session, change_id=CHANGE, cflow_id="CF", case_id="TC-B",
                     verdict=_verdict("TC-B"))
    monkeypatch.undo()

    assert db_session.query(CertFixRound).count() == 1, "the retry duplicated the round"
    assert sorted(rnd.verdict_case_ids) == ["TC-A", "TC-B"]


def test_round_numbers_use_max_plus_one_not_count(db_session):
    """A deleted/renumbered row must not hand out a number already taken."""
    db_session.add(CertFixRound(change_id=CHANGE, round_number=7,
                                status="approved", verdict_case_ids=[], verdicts=[]))
    db_session.commit()
    rnd = open_round(db_session, change_id=CHANGE, cflow_id=None, case_id="TC-A",
                     verdict=_verdict())
    assert rnd.round_number == 8


# ── D5: exactly one notification per round ───────────────────────────────────

def _ready_round(db):
    rnd = open_round(db, change_id=CHANGE, cflow_id="CF", case_id="TC-A",
                     verdict=_verdict())
    mark_round_fixed(CHANGE, rnd.id, user=USER, db=db)
    return rnd


def test_concurrent_approvals_send_exactly_one_notification(db_session, monkeypatch):
    """THE D5 REPRO. The stub RE-ENTERS the endpoint at the exact moment the
    first call is inside the send — the window the two-thread test exposed.
    The re-entrant caller must be refused with 409, and only ONE notification
    may leave.
    """
    sends = []
    reentered = {}

    def fake_send(db, change_id, fixed_case_ids, fix_summary="", ready_for_rerun=True):
        sends.append(fixed_case_ids)
        if "result" not in reentered:            # re-enter exactly once
            try:
                approve_and_notify(CHANGE, rnd.id, user=USER, db=db_session)
                reentered["result"] = "SENT AGAIN"
            except HTTPException as exc:
                reentered["result"] = exc.status_code
        return {"status": "delivered"}

    monkeypatch.setattr(npci_client, "send_cert_fix_notification", fake_send)
    rnd = _ready_round(db_session)
    out = approve_and_notify(CHANGE, rnd.id, user=USER, db=db_session)

    assert len(sends) == 1, f"duplicate cert_fix_notification sent ({len(sends)}×)"
    assert reentered["result"] == 409, \
        "the concurrent approval was not refused — the claim is not atomic"
    assert out["status"] == "approved"


def test_a_second_approval_after_success_is_refused(db_session, monkeypatch):
    sends = []
    monkeypatch.setattr(npci_client, "send_cert_fix_notification",
                        lambda *a, **k: sends.append(1) or {"status": "delivered"})
    rnd = _ready_round(db_session)
    approve_and_notify(CHANGE, rnd.id, user=USER, db=db_session)
    with pytest.raises(HTTPException) as exc:
        approve_and_notify(CHANGE, rnd.id, user=USER, db=db_session)
    assert exc.value.status_code == 409
    assert len(sends) == 1


def test_failed_send_releases_the_claim_for_a_retry(db_session, monkeypatch):
    """The claim must not strand the round in `approving`: a failed send has to
    hand it back so the operator can retry once connectivity returns."""
    monkeypatch.setattr(npci_client, "send_cert_fix_notification",
                        lambda *a, **k: None)
    rnd = _ready_round(db_session)
    with pytest.raises(HTTPException) as exc:
        approve_and_notify(CHANGE, rnd.id, user=USER, db=db_session)
    assert exc.value.status_code == 502
    db_session.expire_all()
    assert db_session.get(CertFixRound, rnd.id).status == "awaiting_approval"


def test_raising_send_also_releases_the_claim(db_session, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("transport exploded")

    monkeypatch.setattr(npci_client, "send_cert_fix_notification", boom)
    rnd = _ready_round(db_session)
    with pytest.raises(HTTPException) as exc:
        approve_and_notify(CHANGE, rnd.id, user=USER, db=db_session)
    assert exc.value.status_code == 502
    db_session.expire_all()
    assert db_session.get(CertFixRound, rnd.id).status == "awaiting_approval"


def test_released_round_can_be_approved_on_retry(db_session, monkeypatch):
    calls = {"n": 0}

    def flaky(db, change_id, fixed_case_ids, **k):
        calls["n"] += 1
        return None if calls["n"] == 1 else {"status": "delivered"}

    monkeypatch.setattr(npci_client, "send_cert_fix_notification", flaky)
    rnd = _ready_round(db_session)
    with pytest.raises(HTTPException):
        approve_and_notify(CHANGE, rnd.id, user=USER, db=db_session)
    out = approve_and_notify(CHANGE, rnd.id, user=USER, db=db_session)
    assert out["status"] == "approved"
    assert calls["n"] == 2
