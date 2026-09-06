# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Contract tests on the Agent interface + registry (WS8)."""
import pytest

from app.agents import registry
from app.agents.base import Agent
from app.models import AgentRun

def test_all_agents_registered_from_manifest():
    # Read the manifest rather than hardcoding a second copy of it. The name of
    # this test is "registered FROM MANIFEST" — the property worth holding is
    # that the loader registers exactly what the manifest declares, not that the
    # roster equals a list frozen when the test was written. The old hardcoded
    # set went stale the moment code_reviewer/security_reviewer were added.
    import yaml
    from pathlib import Path

    manifest = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config" / "agents.yaml").read_text()
    )
    declared = set((manifest.get("agents") or manifest).keys())
    assert set(registry.all_names()) == declared


def test_execute_writes_audit_row_and_returns_dict(db_session):
    out = registry.get("design").execute({"change_title": "X"}, db=db_session, change_id="c1")
    # The output no longer echoes its own agent name — the doc-producing agents
    # return a document payload. Agent identity is carried by the audit row
    # (asserted just below), which is the durable contract.
    assert isinstance(out, dict) and out
    row = db_session.query(AgentRun).filter_by(agent_name="design").one()
    assert row.status == "succeeded"
    assert row.mode == "local"
    assert row.change_id == "c1"
    assert row.latency_ms is not None


def test_failing_agent_is_audited_as_failed(db_session):
    class Boom(Agent):
        def run(self, input):
            raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        Boom("boom").execute({}, db=db_session)
    row = db_session.query(AgentRun).filter_by(agent_name="boom").one()
    assert row.status == "failed"
    assert "kaboom" in (row.error_message or "")


def test_lifecycle_hooks_fire_in_order(db_session):
    calls: list[str] = []

    class Hooked(Agent):
        def before_run(self, input):
            calls.append("before")

        def run(self, input):
            calls.append("run")
            return {"ok": True}

        def after_run(self, input, output):
            calls.append("after")

    Hooked("hooked").execute({}, db=db_session)
    assert calls == ["before", "run", "after"]


def test_feasibility_returns_mock_with_six_areas_without_key(db_session):
    rep = registry.get("feasibility").execute(
        {"change_title": "X", "documents": [{"doc_type": "brd", "content": "y"}]},
        db=db_session,
        change_id="c1",
    )
    assert rep["_meta"].get("mock") is True
    areas = {a["area"] for a in rep["areas"]}
    assert areas == {
        "production_deadline", "scope", "limits",
        "technical_spec", "upstream_dependencies", "certification_role",
    }


def test_doc_agents_return_document_shape(db_session):
    # design/code/test migrated from the flat {agent, status} stub to document
    # payloads. Assert the STRUCTURE they all share, not mock-ness — these
    # assertions must hold whether the agent answered from a live LLM or the
    # mock fallback.
    for name in ("design", "code", "test"):
        out = registry.get(name).execute({"change_title": "X", "topic": "deadline"}, db=db_session)
        assert out["one_line_summary"]
        assert isinstance(out["open_questions"], list)
        assert isinstance(out["_meta"], dict)


def test_negotiation_agent_still_returns_the_flat_stub_shape(db_session):
    # negotiation is the ONE agent not migrated to a document payload; it still
    # answers with the original flat shape. Pinned deliberately so the split is
    # visible rather than looking like an oversight.
    out = registry.get("negotiation").execute(
        {"change_title": "X", "topic": "deadline"}, db=db_session
    )
    assert out["agent"] == "negotiation"
    assert out["kind"] and out["draft_message"] and out["rationale"]
