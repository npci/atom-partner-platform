# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the per-job correlation id context (Finding 13:
security_architecture_skills.md §13.1)."""
from app.core.correlation import current_correlation_id, use_correlation_id


def test_none_outside_any_context():
    assert current_correlation_id() is None


def test_set_within_context():
    with use_correlation_id("job-123"):
        assert current_correlation_id() == "job-123"


def test_reset_after_context_exits():
    with use_correlation_id("job-123"):
        pass
    assert current_correlation_id() is None


def test_nested_contexts_restore_outer_value():
    with use_correlation_id("outer"):
        with use_correlation_id("inner"):
            assert current_correlation_id() == "inner"
        assert current_correlation_id() == "outer"


def test_none_value_is_a_valid_explicit_context():
    with use_correlation_id("outer"):
        with use_correlation_id(None):
            assert current_correlation_id() is None
        assert current_correlation_id() == "outer"


# ── LLM boundary telemetry (SECURITY_ARCHITECTURE.md §7:
# `llm_provider.telemetry.correlation_id_required: true`) ────────────────────

class TestLlmCallLogsCorrelationId:
    """An LLM call must be traceable back to the AgentJob that caused it.
    Without the id on the log line, "which change burned this spend / triggered
    this provider error?" is unanswerable from logs alone."""

    @staticmethod
    def _stub_provider(monkeypatch):
        """Point at a real provider name and stub the SDK call, so the log line
        under test is reached without a network round-trip. Using a fake
        provider name instead would raise before the assertion is meaningful."""
        from app.core import llm

        monkeypatch.setattr(llm.settings, "llm_provider", "claude")
        monkeypatch.setattr(llm.settings, "partner_anthropic_api_key", "k")
        monkeypatch.setattr(llm, "_get_anthropic_client", lambda: object())
        monkeypatch.setattr(llm, "_call_claude", lambda *a, **kw: "ok")

    def test_log_line_includes_the_active_correlation_id(self, monkeypatch, caplog):
        import logging

        from app.core import llm
        from app.core.correlation import use_correlation_id

        self._stub_provider(monkeypatch)

        with caplog.at_level(logging.INFO, logger="app.core.llm"):
            with use_correlation_id("corr-xyz-123"):
                llm.call_llm(system="s", messages=[{"role": "user", "content": "hi"}])

        line = next((r.getMessage() for r in caplog.records if "LLM call:" in r.getMessage()), None)
        assert line is not None, "the LLM call did not log at all"
        assert "correlation_id=corr-xyz-123" in line

    def test_log_line_is_fine_outside_a_job_context(self, monkeypatch, caplog):
        """Called from a script/CLI there is no active job — the field must
        still render (as None) rather than raising."""
        import logging

        from app.core import llm

        self._stub_provider(monkeypatch)
        with caplog.at_level(logging.INFO, logger="app.core.llm"):
            llm.call_llm(system="s", messages=[{"role": "user", "content": "hi"}])

        line = next((r.getMessage() for r in caplog.records if "LLM call:" in r.getMessage()), None)
        assert line is not None
        assert "correlation_id=None" in line
