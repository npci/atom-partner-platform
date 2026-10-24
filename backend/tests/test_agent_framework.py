"""Contract tests on the Agent interface + registry (WS8)."""
import pytest

from app.agents import registry
from app.agents.base import Agent
from app.models import AgentRun

EXPECTED_AGENTS = {"feasibility", "design", "code", "test", "negotiation"}


def test_all_agents_registered_from_manifest():
    assert set(registry.all_names()) == EXPECTED_AGENTS


def test_execute_writes_audit_row_and_returns_dict(db_session):
    out = registry.get("design").execute({"change_title": "X"}, db=db_session, change_id="c1")
    assert isinstance(out, dict) and out["agent"] == "design"
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


def test_stub_agents_return_documented_shape(db_session):
    for name in ("design", "code", "test", "negotiation"):
        out = registry.get(name).execute({"change_title": "X", "topic": "deadline"}, db=db_session)
        assert out["agent"] == name
        assert out["status"] == "mock"
