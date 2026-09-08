# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""End-to-end: inbound change_communication → handler → feasibility agent →
report persisted + agent_runs audit row (WS8)."""
from app.a2a_common.handlers import TaskReceiveRequest, handle_change_communication
from app.a2a_common.handlers._background import _auto_feasibility
from app.models import AgentRun, FeasibilityReport


def test_inbound_change_runs_feasibility_and_audits(db_session):
    body = TaskReceiveRequest(
        task_type="change_communication",
        change_id="NPCI-9",
        payload={
            "change_id": "NPCI-9",
            "title": "UPI Lite top-up",
            "documents": [{"doc_type": "brd", "content": "Body"}],
        },
    )
    res = handle_change_communication(body, db_session)
    local_id = res["local_id"]

    # Run the background feasibility step synchronously (no event loop in tests),
    # exercising the registry → Agent.execute → agent_runs path.
    _auto_feasibility(local_id)

    run = db_session.query(AgentRun).filter_by(agent_name="feasibility").one()
    assert run.status == "succeeded"
    assert run.mode == "local"
    assert run.change_id == local_id

    assert db_session.query(FeasibilityReport).filter_by(change_id=local_id).count() == 1
