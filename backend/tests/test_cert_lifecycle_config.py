# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""CERT-4: the partner's cert config comes from partner_settings, merged over
the active domain pack's fallback.

The location judgement is pinned here: `partner_settings` key `cert_config` —
NOT `partner_profiles`, which holds the PARTNER.md capability document. And
the merge is one level deep so a partial config cannot wipe the nested blocks
it does not mention (`bank_identity` survives a network-only override).

THESE TESTS RUN UNDER THE `upi` PACK. What they exercise is merge and fallback
MECHANICS, and those need a pack that actually supplies a fallback profile. The
default (`generic`) deliberately supplies none — asserting merge behaviour
against an empty fallback would test nothing. The values below are therefore
`upi.yaml`'s, and they are the same literals this handler used to hardcode, so
these assertions still pin exactly what they pinned before.

That the generic pack fabricates NOTHING is the separate, and more important,
guarantee — see `test_unconfigured_generic_deployment_invents_no_identity`.
"""
import json

import pytest

from app.a2a_common.handlers.cert_lifecycle import (
    handle_cert_config_request, handle_cert_setup_notification,
)
from app.a2a_common.handlers._types import TaskReceiveRequest
from app.core.domain import clear_cache
from app.models import ChangeTestData, PartnerSetting

PACKS = "app/packs"


@pytest.fixture(autouse=True)
def _upi_pack(monkeypatch):
    monkeypatch.setenv("DOMAIN_PACK", f"{PACKS}/upi/upi.yaml")
    clear_cache()
    yield
    clear_cache()


def _store(db, value: str):
    db.add(PartnerSetting(key="cert_config", value=value))
    db.commit()


def _body(change_id="chg-1", payload=None):
    return TaskReceiveRequest(task_type="cert_config_request",
                              change_id=change_id, payload=payload or {})


# ── The guarantee the pack seam exists for ───────────────────────────────────

def test_unconfigured_generic_deployment_invents_no_identity(db_session, monkeypatch):
    """An unconfigured deployment must submit nothing, not a fictional bank.

    This handler used to hardcode `ifsc: MYPS0000001`, `handle: @mypsp`,
    `nbin`, `mpinlength: 6` and `supported_protocol_versions: ["UPI 2.x"]`, and
    merged them UNDER the operator's stored config. So the first certification
    handshake of ANY deployment — a library, a telecom, a grid operator —
    asserted a payments identity to its authority, and a deployment that had
    configured only `network` silently inherited the rest.

    The submission is now empty and the summary says so.
    """
    monkeypatch.setenv("DOMAIN_PACK", f"{PACKS}/generic/generic.yaml")
    clear_cache()

    reply = handle_cert_config_request(_body(), db_session)

    assert reply["config"] == {}
    assert "EMPTY" in reply["summary"]

    blob = json.dumps(reply).lower()
    for leaked in ("myps0000001", "mypsp", "olv101", "upi 2.x", "mpinlength",
                   "my bank", "nbin"):
        assert leaked not in blob, f"{leaked!r} reached an unconfigured submission"


def test_unconfigured_generic_deployment_sends_no_case_data(db_session, monkeypatch):
    """Same rule for per-case data: no amount, no MPIN, no payment addresses.

    An empty dict carries no `ready: true`, so cases report as not-ready rather
    than executing against invented values.
    """
    monkeypatch.setenv("DOMAIN_PACK", f"{PACKS}/generic/generic.yaml")
    clear_cache()

    reply = handle_cert_setup_notification(_setup_body(), db_session)

    assert reply["case_data"]["TC1"] == {}
    assert not any(d.get("ready") for d in reply["case_data"].values())


# ── cert_config_request ──────────────────────────────────────────────────────

def test_unconfigured_partner_answers_with_the_packs_fallback(db_session):
    reply = handle_cert_config_request(_body(), db_session)
    assert reply["config"]["psp_org_id"] == "OLV101"
    # The summary names its SOURCE, so an operator reading the conversation can
    # tell a real submission from a fallback without opening the config.
    assert "domain pack" in reply["summary"] and "upi" in reply["summary"]


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


def test_no_rows_at_all_returns_labelled_fallback_values(db_session):
    reply = handle_cert_setup_notification(_setup_body(), db_session)
    assert reply["case_data"]["TC1"]["payerVpa"] == "tester@mypsp"
    assert "domain pack" in reply["summary"]


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
