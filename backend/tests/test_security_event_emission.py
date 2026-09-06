# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Two security events were COMPUTED but never EMITTED: the login lockout
fired and the LLM budget returned a 429, but neither produced a structured,
alertable event (security_architecture_skills.md §13.2/§13.3).

Both were listed as [Target] in SECURITY_ARCHITECTURE.md §13's event catalogue.
"""
import time

import pytest

from app.config import settings


class TestLoginLockoutEvent:
    def _reset(self):
        from app.api import auth
        with auth._failures_lock:
            auth._login_failures.clear()

    def test_lockout_emits_a_structured_event(self, monkeypatch):
        from app.api import auth

        self._reset()
        emitted = []
        monkeypatch.setattr(
            "app.core.security_events.emit_security_event",
            lambda **kw: emitted.append(kw),
        )

        # Drive the failure count past tier 1.
        key = "user:attacker"
        for _ in range(auth._LOCKOUT_TIER_1[0]):
            auth._record_failure(key)

        with pytest.raises(Exception) as exc:
            auth._check_lockout(key)
        assert getattr(exc.value, "status_code", None) == 429

        assert len(emitted) == 1, "lockout fired but emitted no security event"
        ev = emitted[0]
        assert ev["event_name"] == "login_lockout_triggered"
        assert ev["severity"] == "medium"
        assert ev["decision"] == "rejected"
        assert "consecutive_failures" in ev["reason_code"]
        self._reset()

    def test_event_does_not_leak_the_username_or_ip(self, monkeypatch):
        """The lockout key is a username or client IP. Neither belongs in the
        event stream (security_architecture_skills.md §13.4) — the failure
        count is the actionable signal."""
        from app.api import auth

        self._reset()
        emitted = []
        monkeypatch.setattr(
            "app.core.security_events.emit_security_event",
            lambda **kw: emitted.append(kw),
        )

        secret_identity = "victim@bank.example"
        for _ in range(auth._LOCKOUT_TIER_1[0]):
            auth._record_failure(f"user:{secret_identity}")
        with pytest.raises(Exception):
            auth._check_lockout(f"user:{secret_identity}")

        blob = repr(emitted)
        assert secret_identity not in blob, "the lockout event leaked the identity"
        self._reset()

    def test_no_event_when_not_locked_out(self, monkeypatch):
        from app.api import auth

        self._reset()
        emitted = []
        monkeypatch.setattr(
            "app.core.security_events.emit_security_event",
            lambda **kw: emitted.append(kw),
        )
        auth._record_failure("user:someone")  # one failure — under the threshold
        auth._check_lockout("user:someone")   # must not raise
        assert emitted == []
        self._reset()


class TestLlmBudgetEvent:
    def test_budget_exhaustion_emits_a_structured_event(self, db_session, monkeypatch):
        from app.core import llm_budget
        from app.models import AgentJob

        monkeypatch.setattr(settings, "llm_token_budget_per_change", 100)
        db_session.add(AgentJob(change_id="c1", kind="code", status="done", tokens_used=500))
        db_session.commit()

        emitted = []
        monkeypatch.setattr(
            "app.core.security_events.emit_security_event",
            lambda **kw: emitted.append(kw),
        )

        with pytest.raises(llm_budget.TokenBudgetExceeded):
            llm_budget.enforce_budget(db_session, "c1")

        assert len(emitted) == 1, "budget exceeded but emitted no security event"
        ev = emitted[0]
        assert ev["event_name"] == "llm_budget_exceeded"
        assert ev["severity"] == "medium"
        assert ev["boundary"] == "llm_provider"
        assert ev["correlation_id"] == "c1"
        assert "spent=500" in ev["reason_code"]

    def test_no_event_when_under_budget(self, db_session, monkeypatch):
        from app.core import llm_budget
        from app.models import AgentJob

        monkeypatch.setattr(settings, "llm_token_budget_per_change", 10_000)
        db_session.add(AgentJob(change_id="c1", kind="code", status="done", tokens_used=5))
        db_session.commit()

        emitted = []
        monkeypatch.setattr(
            "app.core.security_events.emit_security_event",
            lambda **kw: emitted.append(kw),
        )
        llm_budget.enforce_budget(db_session, "c1")
        assert emitted == []
