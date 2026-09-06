# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CERT-4: the bank's cert config comes from partner_settings, merged over demo.

The location judgement is pinned here: `partner_settings` key `cert_config` —
NOT `partner_profiles`, which holds the PARTNER.md capability document. And
the merge is one level deep so a partial config cannot wipe the nested blocks
it does not mention (`bank_identity` survives a network-only override).
"""
import json

import pytest

from app.a2a_common.handlers.cert_lifecycle import (
    handle_cert_config_request, handle_cert_setup_notification,
)
from app.a2a_common.handlers._types import TaskReceiveRequest
from app.models import ChangeTestData, PartnerSetting


def _store(db, value: str):
    db.add(PartnerSetting(key="cert_config", value=value))
    db.commit()


def _body(change_id="chg-1", payload=None):
    return TaskReceiveRequest(task_type="cert_config_request",
                              change_id=change_id, payload=payload or {})


# ── cert_config_request ──────────────────────────────────────────────────────

def test_unconfigured_bank_answers_with_the_demo_profile(db_session):
    reply = handle_cert_config_request(_body(), db_session)
    assert reply["config"]["psp_org_id"] == "OLV101"
    assert "demo profile" in reply["summary"]


def test_config_comes_from_partner_settings(db_session):
    _store(db_session, json.dumps({"psp_org_id": "REAL01", "bank_code": "RLB"}))
    reply = handle_cert_config_request(_body(), db_session)
    assert reply["config"]["psp_org_id"] == "REAL01"
    assert reply["config"]["bank_code"] == "RLB"
    assert "operator-configured" in reply["summary"]


def test_partial_config_merges_one_level_deep_preserving_bank_identity(db_session):
    """An operator correcting network.host need not restate bank_identity —
    and must not lose the rest of network either."""
    _store(db_session, json.dumps({"network": {"host": "10.0.0.9"}}))
    reply = handle_cert_config_request(_body(), db_session)
    config = reply["config"]
    assert config["network"]["host"] == "10.0.0.9"
    assert config["network"]["port"] == 8443, "unmentioned nested key wiped"
    assert config["bank_identity"]["ifsc"] == "MYPS0000001", "sibling block wiped"


def test_malformed_stored_config_falls_back_to_demo_and_logs_error(db_session, caplog):
    _store(db_session, "{not json")
    with caplog.at_level("ERROR"):
        reply = handle_cert_config_request(_body(), db_session)
    assert reply["config"]["psp_org_id"] == "OLV101"      # demo, not a crash
    assert any("DEMO" in r.message.upper() for r in caplog.records), \
        "the fallback must name its consequence"


def test_stored_non_object_json_is_treated_as_malformed(db_session):
    _store(db_session, json.dumps(["not", "an", "object"]))
    reply = handle_cert_config_request(_body(), db_session)
    assert reply["config"]["psp_org_id"] == "OLV101"


# ── cert_setup_notification ──────────────────────────────────────────────────

def _setup_body(change_id="chg-1", cases=("TC1", "TC2")):
    return TaskReceiveRequest(
        task_type="cert_setup_notification", change_id=change_id,
        payload={"case_list": [{"case_id": tc} for tc in cases]})


def test_no_rows_at_all_returns_labelled_demo_values(db_session):
    reply = handle_cert_setup_notification(_setup_body(), db_session)
    assert reply["case_data"]["TC1"]["payerVpa"] == "tester@mypsp"
    assert "DEMO" in reply["summary"]


def test_cases_with_rows_get_their_stored_data_ready_true(db_session):
    db_session.add(ChangeTestData(change_id="chg-1", tc_id="TC1",
                                  test_data={"payerVpa": "real@bank", "amount": "9.99"}))
    db_session.commit()
    reply = handle_cert_setup_notification(_setup_body(cases=("TC1",)), db_session)
    data = reply["case_data"]["TC1"]
    assert data["payerVpa"] == "real@bank"
    assert data["ready"] is True
    assert "Test Data screen" in reply["summary"]


def test_case_without_a_row_is_ready_false_with_reason(db_session):
    """The bank has STARTED configuring — filling its gaps with demo numbers
    would certify values nobody chose."""
    db_session.add(ChangeTestData(change_id="chg-1", tc_id="TC1",
                                  test_data={"payerVpa": "real@bank"}))
    db_session.commit()
    reply = handle_cert_setup_notification(_setup_body(cases=("TC1", "TC2")), db_session)
    assert reply["case_data"]["TC1"]["ready"] is True
    assert reply["case_data"]["TC2"]["ready"] is False
    assert reply["case_data"]["TC2"]["reason"]


def test_rows_for_another_change_do_not_leak(db_session):
    db_session.add(ChangeTestData(change_id="chg-OTHER", tc_id="TC1",
                                  test_data={"payerVpa": "other@bank"}))
    db_session.commit()
    reply = handle_cert_setup_notification(_setup_body(change_id="chg-1"), db_session)
    assert reply["case_data"]["TC1"]["payerVpa"] == "tester@mypsp"   # demo


def test_a_row_may_explicitly_hold_its_case_back(db_session):
    db_session.add(ChangeTestData(change_id="chg-1", tc_id="TC1",
                                  test_data={"payerVpa": "real@bank", "ready": False}))
    db_session.commit()
    reply = handle_cert_setup_notification(_setup_body(cases=("TC1",)), db_session)
    assert reply["case_data"]["TC1"]["ready"] is False


def test_both_wire_aliases_carry_the_same_values(db_session):
    reply = handle_cert_setup_notification(_setup_body(), db_session)
    assert reply["case_data"] == reply["test_data"]


def test_flat_cases_alias_still_works(db_session):
    body = TaskReceiveRequest(task_type="cert_setup_notification",
                              change_id="chg-1", payload={"cases": ["TC9"]})
    reply = handle_cert_setup_notification(body, db_session)
    assert "TC9" in reply["case_data"]
