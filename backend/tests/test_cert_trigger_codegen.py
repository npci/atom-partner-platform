# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""I-8: codegen emits the certification trigger (§3.5 Stage 2).

The verify bar is "a generated app satisfies the same contract a hand-written
stub did, with NO change on the platform side". String-matching the emitted
source would not show that, so these tests EXECUTE the generated module and
drive its handler through a real FastAPI app — the same calls
`integration_testing/trigger.fire_trigger` makes in Stage 1.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.cert_trigger_codegen import (
    TRIGGER_PATH, case_flow_map, emit_trigger_handler,
)

CASES = {"TC-7": "DISPUTE", "TC-9": "REFUND"}


def _load(files, monkeypatch, *, secret="s3cret"):
    """Execute the emitted module and mount its router — a generated app."""
    module: dict = {}
    exec(compile(files[0]["content"], files[0]["path"], "exec"), module)
    if secret is not None:
        monkeypatch.setenv("CERT_TRIGGER_SECRET", secret)
    else:
        monkeypatch.delenv("CERT_TRIGGER_SECRET", raising=False)
    app = FastAPI()
    app.include_router(module["router"])
    return TestClient(app), module


@pytest.fixture
def emitted():
    return emit_trigger_handler(case_flows=CASES, enabled=True)


# ── guardrail 1: nothing with the flag off ───────────────────────────────────

def test_nothing_is_emitted_with_the_flag_off():
    assert emit_trigger_handler(case_flows=CASES, enabled=False) == []


def test_nothing_is_emitted_when_the_suite_maps_no_cases():
    """A handler that refuses every id is worse than no handler."""
    assert emit_trigger_handler(case_flows={}, enabled=True) == []


# ── guardrail 2: refuses without the bearer secret ───────────────────────────

def test_the_handler_refuses_without_the_bearer_secret(emitted, monkeypatch):
    client, _ = _load(emitted, monkeypatch)
    r = client.post(TRIGGER_PATH, json={"test_case_id": "TC-7"})
    assert r.status_code == 401

    r = client.post(TRIGGER_PATH, json={"test_case_id": "TC-7"},
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_an_unconfigured_secret_refuses_every_call(emitted, monkeypatch):
    """An open trigger lets anyone drive a certification run."""
    client, _ = _load(emitted, monkeypatch, secret=None)
    r = client.post(TRIGGER_PATH, json={"test_case_id": "TC-7"},
                    headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503


def test_the_secret_is_never_baked_into_the_source(emitted):
    assert "s3cret" not in emitted[0]["content"]
    assert "CERT_TRIGGER_SECRET" in emitted[0]["content"]


# ── guardrail 3: the contract itself ─────────────────────────────────────────

def test_a_valid_trigger_is_accepted_with_202_and_never_a_verdict(
        emitted, monkeypatch):
    client, _ = _load(emitted, monkeypatch)
    r = client.post(
        TRIGGER_PATH,
        headers={"Authorization": "Bearer s3cret"},
        json={"test_case_id": "TC-7",
              "cert_context": {"cflow_id": "CF-1", "cert_attempt": 2},
              "case_data": {"amount": "1.00"},
              "reply_via": "a2a://npci_simulator"})
    assert r.status_code == 202
    body = r.json()
    assert body == {"accepted": True, "test_case_id": "TC-7"}
    assert "status" not in body and "passed" not in body and "rc" not in body, \
        "202 says START, never a verdict — a trigger that could report a pass " \
        "would certify itself instead of the implementation"


def test_an_unknown_case_is_refused_not_accepted(emitted, monkeypatch):
    """Accepting it would report 'started' for a case nothing will run."""
    client, _ = _load(emitted, monkeypatch)
    r = client.post(TRIGGER_PATH, headers={"Authorization": "Bearer s3cret"},
                    json={"test_case_id": "TC-NOPE"})
    assert r.status_code == 404


def test_the_emitted_module_carries_the_suites_flow_map(emitted, monkeypatch):
    _, module = _load(emitted, monkeypatch)
    assert module["CASE_FLOWS"] == CASES


def test_the_outbound_call_is_left_to_the_application(emitted, monkeypatch):
    """`originate_case` is deliberately unimplemented: the outbound call is
    the thing under test, and a generated stand-in would certify the
    generator."""
    import asyncio

    _, module = _load(emitted, monkeypatch)
    with pytest.raises(NotImplementedError):
        asyncio.run(module["originate_case"](
            flow="DISPUTE", case_data={}, cert_context={}, reply_via=None))


def test_a_failed_origination_is_logged_not_swallowed(emitted, monkeypatch, caplog):
    """The 202 is already sent, so an exception here would vanish — and a case
    accepted but never originated makes the run wait out its whole deadline."""
    import asyncio
    import logging

    _, module = _load(emitted, monkeypatch)
    with caplog.at_level(logging.ERROR):
        asyncio.run(module["_originate_logged"](
            test_case_id="TC-7", flow="DISPUTE", case_data={},
            cert_context={}, reply_via=None))
    assert any("will never report" in r.getMessage() for r in caplog.records)


# ── the map comes from the suite ─────────────────────────────────────────────

def test_only_cases_this_side_originates_get_a_flow():
    """An authority-initiated case is not something this app starts."""
    assert case_flow_map([
        {"case_id": "TC-1", "initiator": "npci", "api": "ReqPay"},
        {"case_id": "TC-2", "initiator": "bank", "api": "ReqDispute"},
        {"case_id": "TC-3", "initiator": "ISSUER", "api": "ReqRefund"},
        {"case_id": "TC-4", "initiator": "bank"},
    ]) == {"TC-2": "ReqDispute", "TC-3": "ReqRefund"}


def test_the_flag_is_a_declared_setting():
    from app.config import settings

    assert settings.cert_emit_trigger_handler is False


def test_the_generated_path_is_the_contract_path():
    assert TRIGGER_PATH == "/__cert/v1/trigger"


# ── the map comes from the CHANGE's own suite document ───────────────────────
#
# `case_flow_map` is pure and takes rows; these cover the DB half that finds
# them, and the codegen call site that turns them into an emitted file.

SUITE_WITH_API = """
| TC ID | Scenario        | Expected | Initiated By | API        |
|-------|-----------------|----------|--------------|------------|
| TC-1  | authority fires | RC=00    | NPCI         | ReqPay     |
| TC-2  | bank fires      | RC=00    | BANK         | ReqDispute |
| TC-3  | unknown side    | RC=00    |              | ReqRefund  |
"""

# The CANONICAL source shape — `| TC ID | Scenario | Expected |`, no api column.
SUITE_WITHOUT_API = """
| TC ID | Scenario   | Expected | Initiated By |
|-------|------------|----------|--------------|
| TC-2  | bank fires | RC=00    | BANK         |
"""

MODEL_FILES = [{"path": "app/main.py", "content": "# model-authored"}]


def _suite(db, change_id, content):
    from app.models import ChangeDocument

    db.add(ChangeDocument(change_id=change_id, doc_type="cert_test_cases",
                          content=content))
    db.commit()


def _append(db, change_id, files, monkeypatch, *, flag=True):
    from app.api.dashboard import code as code_mod

    monkeypatch.setattr(code_mod.settings, "cert_emit_trigger_handler", flag)
    return code_mod._append_cert_trigger(list(files), change_id, db,
                                         lambda _msg: None)


def test_the_optional_api_column_is_parsed_when_present():
    from app.api.dashboard.certification import _parse_cert_test_cases_md

    rows = {r["tc_id"]: r for r in _parse_cert_test_cases_md(SUITE_WITH_API)}
    assert rows["TC-2"]["api"] == "ReqDispute"
    assert rows["TC-2"]["initiated_by"] == "BANK"


def test_the_canonical_source_shape_carries_no_flow():
    """Guessing one from the scenario prose would originate the wrong call and
    then report it as the right one."""
    from app.api.dashboard.certification import _parse_cert_test_cases_md

    assert [r["api"] for r in _parse_cert_test_cases_md(SUITE_WITHOUT_API)] == [""]


def test_only_bank_initiated_cases_reach_the_flow_map(db_session):
    """TC-1 is authority-initiated; TC-3's initiator cell is blank, which
    defaults to the authority. Neither is something this app originates."""
    from app.api.dashboard.certification import case_flows_for_change

    _suite(db_session, "chg-1", SUITE_WITH_API)
    assert case_flows_for_change(db_session, "chg-1") == {"TC-2": "ReqDispute"}


def test_a_suite_without_an_api_column_maps_nothing(db_session):
    from app.api.dashboard.certification import case_flows_for_change

    _suite(db_session, "chg-2", SUITE_WITHOUT_API)
    assert case_flows_for_change(db_session, "chg-2") == {}


def test_nothing_is_appended_with_the_flag_off(db_session, monkeypatch):
    _suite(db_session, "chg-1", SUITE_WITH_API)
    assert _append(db_session, "chg-1", MODEL_FILES, monkeypatch,
                   flag=False) == MODEL_FILES


def test_the_handler_is_appended_with_the_flag_on(db_session, monkeypatch):
    _suite(db_session, "chg-1", SUITE_WITH_API)
    out = _append(db_session, "chg-1", MODEL_FILES, monkeypatch)
    assert [f["path"] for f in out] == ["app/main.py", "app/cert_trigger.py"]
    assert '"TC-2": "ReqDispute"' in out[-1]["content"]
    assert "TC-1" not in out[-1]["content"]


def test_nothing_is_appended_when_the_suite_maps_no_flows(
        db_session, monkeypatch, caplog):
    """Honest zero-emission, and it says WHY — a handler that refuses every id
    is worse than no handler, but a silent skip is worse than both."""
    import logging

    _suite(db_session, "chg-2", SUITE_WITHOUT_API)
    with caplog.at_level(logging.WARNING):
        out = _append(db_session, "chg-2", MODEL_FILES, monkeypatch)
    assert out == MODEL_FILES
    assert any("NOT emitted" in r.getMessage() for r in caplog.records)


def test_a_model_authored_file_at_the_trigger_path_is_replaced(
        db_session, monkeypatch):
    """Both landing would let one silently shadow the other at import time."""
    _suite(db_session, "chg-1", SUITE_WITH_API)
    clash = [{"path": "app/cert_trigger.py", "content": "# model guess"}]
    out = _append(db_session, "chg-1", clash, monkeypatch)
    assert len(out) == 1
    assert "# model guess" not in out[0]["content"]
    assert "GENERATED, do not edit" in out[0]["content"]
