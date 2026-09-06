# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Smoke tests for each inbound A2A handler (WS8)."""
import json

from app.a2a_common.handlers import (
    TaskReceiveRequest,
    handle_blocker_resolution,
    handle_blocker_status_update,
    handle_cert_test_response,
    handle_change_communication,
    handle_clarification_response,
    handle_counter_decision,
)
from app.models import ChangeDocument, IncomingChange, OutgoingQuery


def _mk_change(db, npci_id="NPCI-1"):
    body = TaskReceiveRequest(
        task_type="change_communication",
        change_id=npci_id,
        payload={
            "change_id": npci_id,
            "title": "Test change",
            "documents": [{"doc_type": "brd", "content": "Body"}],
        },
    )
    return handle_change_communication(body, db)


def test_change_communication_persists(db_session):
    res = _mk_change(db_session)
    assert res["status"] == "accepted"
    assert db_session.query(IncomingChange).count() == 1
    assert db_session.query(ChangeDocument).count() == 1


def test_change_communication_is_idempotent(db_session):
    _mk_change(db_session)
    res2 = _mk_change(db_session)
    assert "duplicate" in res2["message"].lower()
    assert db_session.query(IncomingChange).count() == 1


def test_change_communication_v1_1_product_kit(db_session):
    """v1.1 canonical shape: product_kit[] + attachments[] + kit_version."""
    import base64
    import hashlib

    raw = b"fake-docx-content"
    b64 = base64.b64encode(raw).decode()
    body = TaskReceiveRequest(
        task_type="change_communication",
        change_id="NPCI-V11",
        payload={
            "change_id": "NPCI-V11",
            "kit_version": 1,
            "title": "V1.1 change",
            "product_kit": [
                {
                    "doc_type": "brd", "version": 1, "content": "BRD body",
                    "content_sha256": hashlib.sha256(b"BRD body").hexdigest(),
                    "attachments": [{
                        "kind": "docx", "bytes": b64, "filename": "BRD.docx",
                        "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw),
                        "mime_type": "application/…docx", "omitted": False, "omitted_reason": None,
                    }],
                },
                {"doc_type": "tech_spec", "version": 1, "content": "TSD body",
                 "content_sha256": hashlib.sha256(b"TSD body").hexdigest(), "attachments": []},
            ],
        },
    )
    res = handle_change_communication(body, db_session)
    assert res["status"] == "accepted"
    assert db_session.query(IncomingChange).count() == 1
    assert db_session.query(ChangeDocument).count() == 2
    brd = db_session.query(ChangeDocument).filter_by(doc_type="brd").one()
    assert brd.docx_bytes == raw          # attachments[].bytes decoded from v1.1 shape
    assert brd.docx_filename == "BRD.docx"


def test_change_communication_stores_correlation_id(db_session):
    """The inbound envelope's correlation_id is captured on the change row."""
    body = TaskReceiveRequest(
        task_type="change_communication",
        change_id="NPCI-CORR",
        correlation_id="corr-X",
        payload={"change_id": "NPCI-CORR", "title": "T", "product_kit": []},
    )
    handle_change_communication(body, db_session)
    row = db_session.query(IncomingChange).filter_by(npci_change_id="NPCI-CORR").one()
    assert row.correlation_id == "corr-X"


def test_replies_echo_stored_correlation_id(db_session, monkeypatch):
    """A reply that doesn't set its own payload correlation_id echoes NPCI's
    stored per-(change, bank) thread id on the envelope (v1.1 §5)."""
    from app import npci_client

    db_session.add(IncomingChange(
        id="local-corr-1", npci_change_id="NPCI-CORR", title="T",
        negotiation_version=1, correlation_id="corr-X",
    ))
    db_session.commit()

    captured = {}

    async def _fake_send(**kwargs):
        captured["data"] = kwargs["data"]

    monkeypatch.setattr(npci_client, "authenticate", lambda db: "tok")
    monkeypatch.setattr(npci_client, "_get_a2a_base_url", lambda db: "http://npci.test")
    monkeypatch.setattr(npci_client, "_get_setting", lambda db, k, default="": "")
    monkeypatch.setattr(npci_client, "send_a2a_message", _fake_send)

    # send_blocker sets no payload correlation_id → the stored thread id is used.
    npci_client.send_blocker(db_session, "NPCI-CORR", "B1", "high", "desc")
    assert captured["data"]["correlation_id"] == "corr-X"

    # The query path keeps its own payload correlation_id (OutgoingQuery pointer).
    captured.clear()
    npci_client.send_query(db_session, "NPCI-CORR", "a question", correlation_id="q-row-42")
    assert captured["data"]["correlation_id"] == "q-row-42"


def test_readiness_declaration_payload_spec_shaped(db_session, monkeypatch):
    """v1.1 §cert_readiness_declaration: declared_at/version_implementing present,
    `role` kept for case selection, and `test_data`/`test_data_per_case` dropped
    from the wire — they belong to cert_config_submission."""
    from app import npci_client

    db_session.add(IncomingChange(
        id="local-rd1", npci_change_id="NPCI-RD", title="T", negotiation_version=3,
    ))
    db_session.commit()

    captured = {}

    async def _fake_send(**kwargs):
        captured["data"] = kwargs["data"]

    monkeypatch.setattr(npci_client, "authenticate", lambda db: "tok")
    monkeypatch.setattr(npci_client, "_get_a2a_base_url", lambda db: "http://npci.test")
    monkeypatch.setattr(npci_client, "_get_setting", lambda db, k, default="": "")
    monkeypatch.setattr(npci_client, "send_a2a_message", _fake_send)

    # test_data is still ACCEPTED by the signature but must not reach the wire.
    npci_client.declare_ready(
        db_session, "NPCI-RD", role="PAYER_PSP",
        test_data={"vpa": "a@b"}, test_data_per_case={"RE_1": {"vpa": "a@b"}},
    )

    assert captured["data"]["task_type"] == "cert_readiness_declaration"
    p = captured["data"]["payload"]
    assert p["declared_at"]
    assert p["version_implementing"] == 3
    assert p["implementation_summary"] is None
    assert p["evidence_refs"] == []
    assert p["status"] == "ready_for_cert"          # legacy kept
    assert p["role"] == "PAYER_PSP"                  # kept — selects the case set
    assert "test_data" not in p
    assert "test_data_per_case" not in p


def test_blocker_resolution_v1_1_string_and_legacy_object(db_session):
    """v1.1: `resolution` is a string enum with details at top-level; the handler
    still tolerates the legacy object shape."""
    import json as _json
    from app.a2a_common.handlers import handle_blocker_resolution

    def _seed(npci_id, local_id):
        db_session.add(IncomingChange(
            id=local_id, npci_change_id=npci_id, title="T", negotiation_version=1,
            blockers=_json.dumps([{"blocker_id": "BLK-1", "status": "open", "description": "x"}]),
        ))
        db_session.commit()

    # v1.1 string shape — details at top-level
    _seed("NPCI-BR1", "loc-br1")
    handle_blocker_resolution(TaskReceiveRequest(
        task_type="blocker_resolution", change_id="NPCI-BR1",
        payload={
            "change_id": "NPCI-BR1", "blocker_id": "BLK-1",
            "resolution": "resolved", "resolution_text": "fixed",
            "action_taken": "Patched simulator",
            "patched_artefacts": ["doc://patch.pdf"], "resolved_at": "2026-08-01T15:30:00Z",
        },
    ), db_session)
    b = _json.loads(db_session.query(IncomingChange).filter_by(id="loc-br1").one().blockers)[0]
    assert b["status"] == "resolved"
    assert b["resolution"]["action_taken"] == "Patched simulator"
    assert b["resolution"]["artifact_ref"] == "doc://patch.pdf"     # from patched_artefacts[0]
    assert b["resolution"]["disposition"] == "resolved"

    # legacy object shape still works
    _seed("NPCI-BR2", "loc-br2")
    handle_blocker_resolution(TaskReceiveRequest(
        task_type="blocker_resolution", change_id="NPCI-BR2",
        payload={
            "change_id": "NPCI-BR2", "in_response_to_blocker": "BLK-1",
            "resolution": {"action_taken": "Old shape", "artifact_ref": "a.pdf",
                           "resolved_at": "2026-08-01T10:00:00Z"},
            "resolution_text": "legacy",
        },
    ), db_session)
    b2 = _json.loads(db_session.query(IncomingChange).filter_by(id="loc-br2").one().blockers)[0]
    assert b2["resolution"]["action_taken"] == "Old shape"
    assert b2["resolution"]["disposition"] == "resolved"


def test_blocker_payload_spec_shaped(db_session, monkeypatch):
    """A2A v1.0 §blocker: raised_at populated; type/subject/blocks/references
    defaults present; richer investigation_done/options_considered lists kept."""
    from app import npci_client

    captured = {}

    async def _fake_send(**kwargs):
        captured["data"] = kwargs["data"]

    monkeypatch.setattr(npci_client, "authenticate", lambda db: "tok")
    monkeypatch.setattr(npci_client, "_get_a2a_base_url", lambda db: "http://npci.test")
    monkeypatch.setattr(npci_client, "_get_setting", lambda db, k, default="": "")
    monkeypatch.setattr(npci_client, "send_a2a_message", _fake_send)

    npci_client.send_blocker(
        db_session, "NPCI-B", "BLK-1", "high", "Simulator non-deterministic",
        impact="Blocks RE_5", investigation_done=["24h traces"],
        options_considered=[{"option": "route around", "eta": None, "impact": "risky"}],
        requested_action_from_npci="Fix simulator",
    )

    p = captured["data"]["payload"]
    assert p["blocker_id"] == "BLK-1"
    assert p["raised_at"]
    assert p["type"] is None and p["subject"] is None
    assert p["blocks"] == [] and p["references"] == [] and p["evidence_refs"] == []
    assert p["investigation_done"] == ["24h traces"]              # richer list kept
    assert p["options_considered"][0]["option"] == "route around"  # structured kept


def test_milestone_update_payload_spec_shaped(db_session, monkeypatch):
    """A2A v1.0 §milestone_update: version_implementing/completed_at/next_milestone
    populated; evidence_refs/risks default; state=completed + notes kept."""
    from app import npci_client

    db_session.add(IncomingChange(
        id="local-m1", npci_change_id="NPCI-M", title="T", negotiation_version=2,
    ))
    db_session.commit()

    captured = {}

    async def _fake_send(**kwargs):
        captured.setdefault("sends", []).append(kwargs["data"])

    monkeypatch.setattr(npci_client, "authenticate", lambda db: "tok")
    monkeypatch.setattr(npci_client, "_get_a2a_base_url", lambda db: "http://npci.test")
    monkeypatch.setattr(npci_client, "_get_setting", lambda db, k, default="": "")
    monkeypatch.setattr(npci_client, "send_a2a_message", _fake_send)

    npci_client.report_progress(db_session, "NPCI-M", "design_completed", notes="Board reviewed")
    npci_client.report_progress(db_session, "NPCI-M", "testing_completed")

    design = captured["sends"][0]["payload"]
    assert design["milestone"] == "design"
    assert design["state"] == "completed"
    assert design["version_implementing"] == 2
    assert design["next_milestone"] == "coding"
    assert design["completed_at"]
    assert design["evidence_refs"] == [] and design["risks"] == []
    assert design["notes"] == "Board reviewed"

    testing = captured["sends"][1]["payload"]
    assert testing["next_milestone"] is None          # last milestone
    assert "notes" not in testing                      # omitted when empty


def test_query_payload_spec_shaped(db_session, monkeypatch):
    """A2A v1.0 §query: query_id/question/asked_at/phase present; legacy
    `message` kept for the NPCI executor; cert variant sets phase=cert."""
    from app import npci_client

    captured = {}

    async def _fake_send(**kwargs):
        captured.setdefault("sends", []).append(kwargs["data"])

    monkeypatch.setattr(npci_client, "authenticate", lambda db: "tok")
    monkeypatch.setattr(npci_client, "_get_a2a_base_url", lambda db: "http://npci.test")
    monkeypatch.setattr(npci_client, "_get_setting", lambda db, k, default="": "")
    monkeypatch.setattr(npci_client, "send_a2a_message", _fake_send)

    npci_client.send_query(db_session, "NPCI-Q", "Timeout end-to-end?", correlation_id="q-1")
    npci_client.send_cert_query(db_session, "NPCI-Q", "Cert env ready?", correlation_id="q-2")

    gen = captured["sends"][0]["payload"]
    assert gen["query_id"] == "q-1"
    assert gen["question"] == "Timeout end-to-end?"
    assert gen["message"] == "Timeout end-to-end?"   # legacy kept (NPCI reads this)
    assert gen["phase"] is None
    assert gen["priority"] == "normal"
    assert gen["evidence_refs"] == [] and gen["references"] == []

    cert = captured["sends"][1]["payload"]
    assert cert["phase"] == "cert"
    assert cert["query_id"] == "q-2"


def test_counter_proposal_payload_spec_shaped(db_session, monkeypatch):
    """A2A v1.0 §counter_proposal: version_targeted + counters[] + summary/
    references present; in_response_to:kit_id dropped; legacy fields kept."""
    from app import npci_client

    db_session.add(IncomingChange(
        id="local-cp-1", npci_change_id="NPCI-CP", title="T", negotiation_version=2,
    ))
    db_session.commit()

    captured = {}

    async def _fake_send(**kwargs):
        captured["data"] = kwargs["data"]

    monkeypatch.setattr(npci_client, "authenticate", lambda db: "tok")
    monkeypatch.setattr(npci_client, "_get_a2a_base_url", lambda db: "http://npci.test")
    monkeypatch.setattr(npci_client, "_get_setting", lambda db, k, default="": "")
    monkeypatch.setattr(npci_client, "send_a2a_message", _fake_send)

    npci_client.send_counter_proposal(
        db_session, "NPCI-CP", "CHG_abc123", "cp-1",
        justification="Peak p99 is 6.8s; need 8s.", negotiation_round=1,
    )

    p = captured["data"]["payload"]
    assert p["version_targeted"] == 2
    assert "in_response_to" not in p                       # dropped
    assert p["kit_id"] == "CHG_abc123"
    assert isinstance(p["counters"], list) and len(p["counters"]) == 1
    assert p["counters"][0]["rationale"] == "Peak p99 is 6.8s; need 8s."
    assert p["summary"] == "Peak p99 is 6.8s; need 8s."
    assert p["references"] == []
    assert p["justification"] == "Peak p99 is 6.8s; need 8s."   # legacy kept
    assert p["negotiation_round"] == 1                          # extension kept


def test_change_acknowledgement_payload_carries_version_accepted(db_session, monkeypatch):
    """A2A v1.0 §change_acknowledgement: version_accepted present (from the held
    kit version), no stray in_response_to, enrichments preserved."""
    from app import npci_client

    db_session.add(IncomingChange(
        id="local-acc-1", npci_change_id="NPCI-ACC", title="T",
        negotiation_version=2, correlation_id="corr-X",
    ))
    db_session.commit()

    captured = {}

    async def _fake_send(**kwargs):
        captured["data"] = kwargs["data"]

    monkeypatch.setattr(npci_client, "authenticate", lambda db: "tok")
    monkeypatch.setattr(npci_client, "_get_a2a_base_url", lambda db: "http://npci.test")
    monkeypatch.setattr(npci_client, "_get_setting", lambda db, k, default="": "")
    monkeypatch.setattr(npci_client, "send_a2a_message", _fake_send)

    npci_client.send_proposal_acceptance(
        db_session, "NPCI-ACC", "CHG_abc123",
        accepted_by={"role": "cert_lead", "name": "Priya", "email": "p@bankx"},
        internal_change_advisory_ref="HDFC-CAB-1",
        estimated_phase_timeline={"go_live": "2026-08-31"},
        implementation_kickoff_date="2026-08-01",
    )

    p = captured["data"]["payload"]
    assert p["version_accepted"] == 2                 # from held kit version
    assert "in_response_to" not in p                  # dropped (not a spec field here)
    assert p["kit_id"] == "CHG_abc123"
    assert p["decision"] == "ACCEPT"
    assert p["internal_change_advisory_ref"] == "HDFC-CAB-1"      # enrichment kept
    assert p["implementation_kickoff_date"] == "2026-08-01"
    assert p["estimated_phase_timeline"] == {"go_live": "2026-08-31"}
    assert captured["data"]["correlation_id"] == "corr-X"         # thread echo still works


def test_proposal_acknowledged_payload_conforms_to_spec(db_session, monkeypatch):
    """A2A v1.0 §proposal_acknowledged: in_response_to = inbound message_id,
    version_received present, kit_files_received is a doc_type string list, plus
    the additive kit_files_verified checksum receipt."""
    from app import npci_client

    captured = {}

    def _fake_send_task(db, task_type, change_id, payload):
        captured.update(task_type=task_type, change_id=change_id, payload=payload)
        return {"status": "delivered"}

    monkeypatch.setattr(npci_client, "send_task", _fake_send_task)

    kit_files = [
        {"doc_type": "brd", "kind": "content", "checksum_verified": True},
        {"doc_type": "brd", "kind": "docx", "name": "BRD.docx", "checksum_verified": True},
        {"doc_type": "tech_spec", "kind": "content", "checksum_verified": True},
    ]
    npci_client.send_proposal_acknowledged(
        db_session, "NPCI-1", "CHG_abc123", 2, "msg-N-001", kit_files,
    )

    assert captured["task_type"] == "proposal_acknowledged"
    p = captured["payload"]
    assert p["in_response_to"] == "msg-N-001"            # inbound message_id, not kit_id
    assert p["version_received"] == 2
    assert p["review_phase"] == "feasibility"
    assert p["kit_files_received"] == ["brd", "tech_spec"]   # unique doc_type strings
    assert p["kit_files_verified"] == kit_files              # checksum receipt preserved
    assert p["kit_id"] == "CHG_abc123"


def test_clarification_response_matches_by_query_id(db_session):
    """v1.1: NPCI's clarification_response carries the spec's `query_id` (not
    `correlation_id`); the partner still matches the exact OutgoingQuery."""
    db_session.add(IncomingChange(
        id="loc-q1", npci_change_id="NPCI-Q", title="T", negotiation_version=1,
    ))
    db_session.add(OutgoingQuery(
        id="oq1", change_id="loc-q1", message="Timeout end-to-end?",
        status="sent", kind="general", correlation_id="q-corr-1",
    ))
    db_session.commit()

    body = TaskReceiveRequest(
        task_type="clarification_response",
        change_id="NPCI-Q",
        payload={
            "change_id": "NPCI-Q",
            "query_id": "q-corr-1",          # spec field, no correlation_id present
            "response": "End-to-end at the issuer switch.",
            "channel": "general",
        },
    )
    handle_clarification_response(body, db_session)

    q = db_session.query(OutgoingQuery).filter_by(id="oq1").one()
    assert q.status == "answered"
    assert q.response == "End-to-end at the issuer switch."


def test_clarification_counter_proposal_sets_negotiating(db_session):
    _mk_change(db_session)
    body = TaskReceiveRequest(
        task_type="clarification_response",
        change_id="NPCI-1",
        payload={"payload": {
            "change_id": "NPCI-1", "message_kind": "COUNTER_PROPOSAL",
            "counter_proposal_id": "cp1", "negotiation_round": 1, "justification": "because",
        }},
    )
    handle_clarification_response(body, db_session)
    ch = db_session.query(IncomingChange).filter_by(npci_change_id="NPCI-1").one()
    assert ch.decision == "negotiating"
    assert ch.npci_counter is not None


def test_counter_decision_first_class_records(db_session):
    """Protocol v1: NPCI's ACCEPT/REJECT arrives as a first-class
    counter_decision task type (no longer clarification_response+message_kind)."""
    res = _mk_change(db_session)
    q = OutgoingQuery(
        change_id=res["local_id"], message="we counter X", status="sent",
        kind="negotiation",
    )
    db_session.add(q)
    db_session.commit()
    body = TaskReceiveRequest(
        task_type="counter_decision",
        change_id="NPCI-1",
        payload={"payload": {
            "change_id": "NPCI-1", "decision": "ACCEPT",
            "in_response_to": "cp1", "negotiation_round": 1, "resolution_text": "agreed",
        }},
    )
    handle_counter_decision(body, db_session)
    ch = db_session.query(IncomingChange).filter_by(npci_change_id="NPCI-1").one()
    db_session.refresh(q)
    cds = json.loads(ch.counter_decisions)
    assert cds[-1]["decision"] == "ACCEPT"
    assert q.status == "accepted"
    assert q.response == "agreed"


def test_clarification_regular_answers_query(db_session):
    res = _mk_change(db_session)
    q = OutgoingQuery(
        change_id=res["local_id"], message="Q?", status="sent",
        kind="general", correlation_id="corr1",
    )
    db_session.add(q)
    db_session.commit()
    body = TaskReceiveRequest(
        task_type="clarification_response",
        change_id="NPCI-1",
        payload={"payload": {"change_id": "NPCI-1", "correlation_id": "corr1", "response": "A!"}},
    )
    handle_clarification_response(body, db_session)
    db_session.refresh(q)
    assert q.status == "answered"
    assert q.response == "A!"


def test_blocker_resolution_patches_blocker(db_session):
    _mk_change(db_session)
    ch = db_session.query(IncomingChange).filter_by(npci_change_id="NPCI-1").one()
    ch.blockers = json.dumps([{"blocker_id": "b1", "status": "open"}])
    db_session.commit()
    body = TaskReceiveRequest(
        task_type="blocker_resolution",
        change_id="NPCI-1",
        payload={"payload": {
            "change_id": "NPCI-1", "in_response_to_blocker": "b1",
            "resolution": {"action_taken": "fixed"}, "resolution_text": "done",
        }},
    )
    handle_blocker_resolution(body, db_session)
    db_session.refresh(ch)
    blk = json.loads(ch.blockers)[0]
    assert blk["status"] == "resolved"
    assert blk["resolution"]["action_taken"] == "fixed"


def test_blocker_status_update_patches_non_terminal(db_session):
    _mk_change(db_session)
    ch = db_session.query(IncomingChange).filter_by(npci_change_id="NPCI-1").one()
    ch.blockers = json.dumps([{"blocker_id": "b1", "status": "open"}])
    db_session.commit()
    body = TaskReceiveRequest(
        task_type="blocker_status_update",
        change_id="NPCI-1",
        payload={"payload": {
            "change_id": "NPCI-1", "in_response_to_blocker": "b1",
            "status": "in_investigation", "assigned_team": "simulator_ops",
            "crm": {"ticket_id": "INC-1"}, "notes": "looking into it",
        }},
    )
    handle_blocker_status_update(body, db_session)
    db_session.refresh(ch)
    blk = json.loads(ch.blockers)[0]
    # Non-terminal: status advances but the blocker is NOT resolved.
    assert blk["status"] == "in_investigation"
    assert blk["assigned_team"] == "simulator_ops"
    assert blk["status_history"][-1]["crm"]["ticket_id"] == "INC-1"
    assert "resolution" not in blk


def test_cert_test_response_records_summary_and_certifies(db_session):
    _mk_change(db_session)
    body = TaskReceiveRequest(
        task_type="cert_test_response",
        change_id="NPCI-1",
        payload={"cert_run_id": "r1", "total": 2, "passed": 2, "failed": 0, "results": []},
    )
    handle_cert_test_response(body, db_session)
    ch = db_session.query(IncomingChange).filter_by(npci_change_id="NPCI-1").one()
    assert ch.cert_summary is not None
    assert ch.cert_status == "certified"


# ── Dispatch-registry guards (retrofit PR-1) ─────────────────────────────────
# The cert lifecycle handlers were wired, then silently unwired by an unrelated
# merge, TWICE. Nothing failed either time: handlers.get() returned None, the
# type was already in _INBOUND_TASK_TYPES, and the executor answered with a
# generic ack — HTTP 200, status=accepted, correct at every layer except the
# conversation the user was watching. These two make that failure loud.

def test_every_declared_task_type_has_a_handler():
    """The dispatch registry must cover every task type we claim to handle."""
    from app.a2a_common.partner_executor import HANDLER_TASK_TYPES, build_handler_registry

    registry = build_handler_registry()
    missing = HANDLER_TASK_TYPES - registry.keys()
    assert not missing, f"declared but not dispatched: {sorted(missing)}"

    undeclared = registry.keys() - HANDLER_TASK_TYPES
    assert not undeclared, f"dispatched but not declared: {sorted(undeclared)}"


def test_cert_task_types_are_recognised_inbound():
    """A dispatched type must also be recognised, or the ack branch misreports it."""
    from app.a2a_common.partner_executor import _INBOUND_TASK_TYPES, build_handler_registry

    missing = build_handler_registry().keys() - _INBOUND_TASK_TYPES
    assert not missing, f"handled but not in _INBOUND_TASK_TYPES: {sorted(missing)}"


# ── Cert lifecycle behaviour (retrofit PR-3, upstream 5d0e3827) ──────────────
# These are the six that PR-1 deliberately left out: their implementation ships
# with this commit (spec-shaped config + per-case readiness), not with the
# dispatch restore.

def test_config_request_returns_the_banks_config(db_session):
    """The orchestrator reads response_body['config'] to onboard the bank in precert."""
    from app.a2a_common.handlers import handle_cert_config_request

    body = TaskReceiveRequest(
        task_type="cert_config_request", change_id="NPCI-1",
        payload={"summary": "Please submit your certification configuration parameters."},
    )
    out = handle_cert_config_request(body, db_session)
    cfg = out["config"]
    assert out["task_type"] == "cert_config_submission"

    # Spec shape (Appendix B cert_config_submission).
    for k in ("renews_cflow_id", "bank_identity", "network", "security", "roles",
              "supported_protocol_versions", "supported_features", "requested_subset",
              "contacts", "preferred_window"):
        assert k in cfg, f"spec field {k} missing"
    assert cfg["bank_identity"]["org_id"] == "MYORG1"
    assert cfg["network"]["port"] == 8443

    # The bank chooses its own suite — NPCI's default only applies if this is
    # absent or names a subset that does not exist.
    assert cfg["requested_subset"] == "Subset-A2A-BIDI"

    # Extension keys precert needs and the spec has no home for.
    assert cfg["psp_org_id"] == "OLV101"
    assert cfg["bank_code"] == "MYB"
    # Must route to the running bank simulator, or the cert run times out waiting
    # on a row in upihosttxnlog.
    assert cfg["bank_server_port"] == "8443"


def test_config_carries_no_certificate_bodies(db_session):
    """Spec: certificate bodies are NEVER inline — only cert_ref + fingerprint."""
    from app.a2a_common.handlers import handle_cert_config_request

    sec = handle_cert_config_request(
        TaskReceiveRequest(task_type="cert_config_request", change_id="NPCI-1",
                           payload={"summary": "x"}), db_session)["config"]["security"]
    assert sec["hsm_certificate"] is None
    assert sec["ssl_signer"] is None and sec["ssl_client_cert"] is None


def test_setup_notification_returns_per_case_data(db_session):
    """The orchestrator feeds response_body['case_data'][tc] in as CaseSpec.overrides."""
    from app.a2a_common.handlers import handle_cert_setup_notification

    body = TaskReceiveRequest(
        task_type="cert_setup_notification", change_id="NPCI-1",
        payload={"subset": "Subset-A2A-BIDI", "cases": ["RE_94", "RE_01"]},
    )
    out = handle_cert_setup_notification(body, db_session)
    assert out["task_type"] == "cert_test_preparation"
    # `case_data` is the spec key; `test_data` stays as an alias for an NPCI side
    # that has not picked up the rename.
    assert set(out["case_data"]) == {"RE_94", "RE_01"}
    assert out["test_data"] == out["case_data"]
    for tc_data in out["case_data"].values():
        # These four keys are real fields on precert's ACQData VO, and connector._fire
        # merges them LAST — so they win over _DEFAULT_BODY.
        assert {"amount", "mpin", "payerVpa", "payeeVpa"} <= tc_data.keys()
        # `ready` is the spec's execution gate; NPCI skips a case declared false.
        assert tc_data["ready"] is True


def test_setup_notification_prefers_the_spec_case_list(db_session):
    """`case_list` is the spec's field; `cases` is the platform's flat alias."""
    from app.a2a_common.handlers import handle_cert_setup_notification

    out = handle_cert_setup_notification(TaskReceiveRequest(
        task_type="cert_setup_notification", change_id="NPCI-1",
        payload={"case_list": [{"case_id": "BE_22"}, {"case_id": "BE_23"}],
                 "cases": ["IGNORED"]},
    ), db_session)
    assert set(out["case_data"]) == {"BE_22", "BE_23"}


def test_case_result_reports_back_only_for_bank_initiated_cases(db_session):
    """Spec: cert_case_result is bidirectional; the bank reports its own cases."""
    from app.a2a_common.handlers import handle_cert_case_result

    bank = handle_cert_case_result(TaskReceiveRequest(
        task_type="cert_case_result", change_id="NPCI-1",
        payload={"test_case_id": "RE_94", "status": "PASS", "reporter": "bank"},
    ), db_session)
    assert bank["reporter"] == "bank"
    assert bank["test_case_id"] == "RE_94"

    npci = handle_cert_case_result(TaskReceiveRequest(
        task_type="cert_case_result", change_id="NPCI-1",
        payload={"test_case_id": "RE_94", "status": "PASS", "reporter": "npci"},
    ), db_session)
    # Silent ack — no task_type/summary, so CertConversation renders no bank bubble.
    assert "task_type" not in npci


def test_verdict_notification_requests_a_waiver(db_session):
    from app.a2a_common.handlers import handle_cert_verdict_notification

    out = handle_cert_verdict_notification(TaskReceiveRequest(
        task_type="cert_verdict_notification", change_id="NPCI-1",
        payload={"test_case_id": "RE_87", "classification": "waiver_eligible"},
    ), db_session)
    assert out["task_type"] == "cert_waiver_request"
    assert out["test_case_id"] == "RE_87"
    # Spec splits these: `category` is the enum NPCI records the waiver under,
    # `reason` the prose. Sending prose as the category is what the old single
    # field did.
    assert out["category"] == "non_applicable"
    assert out["reason"] and out["reason"] != out["category"]
