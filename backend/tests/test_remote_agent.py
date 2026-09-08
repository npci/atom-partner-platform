# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""RemoteAgent (url: binding) tests + the reserved mcp: binding (WS8)."""
import httpx
import pytest

from app.agents import loader
from app.agents.remote import RemoteAgent
from app.models import AgentRun


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_remote_agent_success_records_http_mode(db_session, monkeypatch):
    def fake_post(url, json, headers, timeout):
        assert headers.get("X-Correlation-Id")  # contract: correlation id sent
        return _FakeResp(200, {"status": "ok", "output": {"agent": "code", "status": "ok"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    out = RemoteAgent("code", url="http://bank/agents/code", retries=0).execute(
        {"change_id": "c1"}, db=db_session, change_id="c1"
    )
    assert out == {"agent": "code", "status": "ok"}
    row = db_session.query(AgentRun).filter_by(agent_name="code").one()
    assert row.mode == "http"
    assert row.status == "succeeded"
    assert row.http_status == 200
    assert row.endpoint == "http://bank/agents/code"


def test_remote_agent_error_payload_is_failed(db_session, monkeypatch):
    monkeypatch.setattr(
        httpx, "post",
        lambda url, json, headers, timeout: _FakeResp(200, {"status": "error", "error": "boom"}),
    )
    with pytest.raises(RuntimeError):
        RemoteAgent("code", url="http://bank/x", retries=0).execute({}, db=db_session)
    row = db_session.query(AgentRun).filter_by(agent_name="code").one()
    assert row.status == "failed"


def test_mcp_binding_is_reserved_not_built(tmp_path, monkeypatch):
    manifest = tmp_path / "agents.yaml"
    manifest.write_text("agents:\n  code:\n    mcp: {server: http://x/mcp, tool: t}\n")
    monkeypatch.setenv("AGENTS_CONFIG", str(manifest))
    with pytest.raises(NotImplementedError):
        loader.build_registry()
