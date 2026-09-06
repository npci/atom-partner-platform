# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""The cert handlers must key partner-side stores on the LOCAL change id.

An A2A envelope carries the AUTHORITY's `change_id`. Every partner-side store
— `change_test_data`, `cert_fix_rounds` — is keyed on `IncomingChange.id`,
because that is what the dashboard routes carry. The two are different values.

The cert lifecycle handlers used to query with the envelope's id directly. The
failure was SILENT and cost a whole certification round: the operator filled in
the Test Data screen, the PUT returned 200, and `cert_setup_notification` still
answered "no test data configured" while `cert_execution_start` dispatched every
case with empty `case_data` — the rig then failed all of them for missing
fields that were sitting in the database under the other key.

EVERY TEST HERE USES A LOCAL ID THAT DIFFERS FROM THE NPCI ID. That is the
whole point: the pre-existing suite passed `change_id="chg-1"` for both, so the
two keys coincided and the bug was invisible. Any future test that reuses one
value for both is not testing this.
"""
from __future__ import annotations

import asyncio

import pytest

from app.a2a_common.handlers._types import TaskReceiveRequest
from app.a2a_common.handlers.cert_lifecycle import (
    handle_cert_execution_start,
    handle_cert_setup_notification,
    handle_cert_verdict_notification,
)
from app.models import CertFixRound, ChangeTestData, IncomingChange, PartnerSetting

LOCAL_ID = "local-aaaa-1111"
NPCI_ID = "npci-bbbb-2222"


@pytest.fixture()
def change(db_session):
    row = IncomingChange(id=LOCAL_ID, npci_change_id=NPCI_ID, title="A change")
    db_session.add(row)
    db_session.commit()
    return row


def _setup_body(cases, change_id=NPCI_ID):
    return TaskReceiveRequest(
        task_type="cert_setup_notification", change_id=change_id,
        payload={"case_list": [{"case_id": c} for c in cases]})


def _start_body(case_ids, change_id=NPCI_ID):
    return TaskReceiveRequest(
        task_type="cert_execution_start", change_id=change_id,
        payload={"case_ids": case_ids, "simulator_alias": "cert_simulator",
                 "cert_context": {"cflow_id": "CF-1", "cert_attempt": 1}})


# ── the read path finds what the Test Data screen wrote ──────────────────────

def test_setup_notification_reads_rows_written_under_the_local_id(db_session, change):
    """The row is stored under LOCAL_ID; the envelope announces NPCI_ID."""
    db_session.add(ChangeTestData(change_id=LOCAL_ID, tc_id="TC1",
                                  test_data={"account": "42", "ready": True}))
    db_session.commit()

    reply = handle_cert_setup_notification(_setup_body(["TC1"]), db_session)

    assert reply["case_data"]["TC1"]["account"] == "42", \
        "the operator's value must reach the authority, not the demo fallback"
    assert "Test Data screen" in reply["summary"]


def test_a_case_without_a_row_is_still_reported_not_ready(db_session, change):
    """The resolution must not turn every case into a demo-default pass —
    a change with SOME rows still reports the gaps honestly."""
    db_session.add(ChangeTestData(change_id=LOCAL_ID, tc_id="TC1",
                                  test_data={"account": "42"}))
    db_session.commit()

    reply = handle_cert_setup_notification(_setup_body(["TC1", "TC2"]), db_session)

    assert reply["case_data"]["TC1"]["ready"] is True
    assert reply["case_data"]["TC2"]["ready"] is False
    assert "no test data configured" in reply["case_data"]["TC2"]["reason"]


def test_execution_start_dispatches_the_operators_case_data(db_session, change, monkeypatch):
    db_session.add(PartnerSetting(key="cert_trigger_url",
                                  value="https://sut.example/__cert/v1/trigger"))
    db_session.add(ChangeTestData(change_id=LOCAL_ID, tc_id="TC1",
                                  test_data={"account": "42"}))
    db_session.commit()

    fired: list[dict] = []
    monkeypatch.setattr(
        "app.services.integration_testing.trigger.fire_trigger",
        lambda url, secret, **kw: fired.append(kw) or True)

    async def scenario():
        reply = handle_cert_execution_start(_start_body(["TC1"]), db_session)
        await asyncio.sleep(0.05)   # let the _spawn'd workers run
        return reply

    assert asyncio.run(scenario())["dispatched"] == 1
    assert fired[0]["case_data"] == {"account": "42"}, \
        "empty case_data here is the defect: the rig fails the case for a " \
        "missing field that the Test Data screen already holds"


def test_the_rig_is_still_told_the_authoritys_change_id(db_session, change, monkeypatch):
    """`cert_context.npci_change_id` travels back OUT on the wire, where only
    the authority's id resolves. Resolving the local id for store lookups must
    not leak that local id into the outbound context."""
    db_session.add(PartnerSetting(key="cert_trigger_url", value="https://sut.example/t"))
    db_session.commit()

    fired: list[dict] = []
    monkeypatch.setattr(
        "app.services.integration_testing.trigger.fire_trigger",
        lambda url, secret, **kw: fired.append(kw) or True)

    async def scenario():
        handle_cert_execution_start(_start_body(["TC1"]), db_session)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert fired[0]["cert_context"]["npci_change_id"] == NPCI_ID


# ── the write path and the read path agree end to end ────────────────────────

def test_test_data_screen_write_is_visible_to_the_cert_handler(db_session, change):
    """The real defect, exercised through both real code paths rather than a
    hand-placed row: save through the endpoint the UI calls, then read through
    the handler the authority triggers."""
    from app.api.dashboard.certification import TestDataUpsert, upsert_test_data

    upsert_test_data(
        change_id=LOCAL_ID, tc_id="TC1",
        body=TestDataUpsert(test_data={"account": "42"}),
        user=None, db=db_session,
    )

    reply = handle_cert_setup_notification(_setup_body(["TC1"]), db_session)
    assert reply["case_data"]["TC1"]["account"] == "42"


# ── fix rounds land where the dashboard looks for them ───────────────────────

def test_verdict_opens_the_fix_round_under_the_local_id(db_session, change, monkeypatch):
    """`cert_fix.py` lists rounds by the id its own routes carry (the local
    one). A round opened under the authority's id exists but is invisible."""
    monkeypatch.setattr("app.a2a_common.handlers._background._spawn",
                        lambda fn, *a, **kw: None)

    reply = handle_cert_verdict_notification(
        TaskReceiveRequest(
            task_type="cert_verdict_notification", change_id=NPCI_ID,
            payload={"test_case_id": "TC1", "classification": "real_defect",
                     "cflow_id": "CF-1", "assertion_failures": [{"field": "x"}]}),
        db_session)

    assert reply["task_type"] == "cert_defect_ack"
    rounds = db_session.query(CertFixRound).all()
    assert [r.change_id for r in rounds] == [LOCAL_ID]


# ── the fallback is honest, not silent ───────────────────────────────────────

def test_an_unknown_change_falls_back_to_the_envelope_id(db_session, caplog):
    """No IncomingChange row: keep using the authority's id rather than
    addressing some other change, and say so in the log."""
    import logging

    db_session.add(ChangeTestData(change_id=NPCI_ID, tc_id="TC1",
                                  test_data={"account": "42"}))
    db_session.commit()

    with caplog.at_level(logging.WARNING):
        reply = handle_cert_setup_notification(_setup_body(["TC1"]), db_session)

    assert reply["case_data"]["TC1"]["account"] == "42"
    assert "no IncomingChange for npci_change_id" in caplog.text
