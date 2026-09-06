# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the per-change LLM token budget guard (Finding 4:
security_architecture_skills.md §4.2, EA_Skills.md P6/P10)."""
import pytest

from app.config import settings
from app.core.llm_budget import TokenBudgetExceeded, enforce_budget, tokens_spent_for_change
from app.models import AgentJob


def _add_job(db_session, change_id: str, tokens: int, status: str = "done") -> AgentJob:
    job = AgentJob(change_id=change_id, kind="code", status=status, tokens_used=tokens)
    db_session.add(job)
    db_session.commit()
    return job


class TestTokensSpentForChange:
    def test_sums_across_multiple_jobs(self, db_session):
        _add_job(db_session, "c1", 1000)
        _add_job(db_session, "c1", 2500)
        assert tokens_spent_for_change(db_session, "c1") == 3500

    def test_ignores_other_changes(self, db_session):
        _add_job(db_session, "c1", 1000)
        _add_job(db_session, "c2", 9000)
        assert tokens_spent_for_change(db_session, "c1") == 1000

    def test_zero_for_unknown_change(self, db_session):
        assert tokens_spent_for_change(db_session, "does-not-exist") == 0

    def test_null_tokens_used_treated_as_zero(self, db_session):
        # A job that predates token tracking, or one whose runner never
        # called an LLM (e.g. the MR-push job) — tokens_used stays NULL.
        _add_job(db_session, "c1", None)  # type: ignore[arg-type]
        assert tokens_spent_for_change(db_session, "c1") == 0

    def test_includes_failed_jobs(self, db_session):
        # Tokens spent before a job failed still count against the budget.
        _add_job(db_session, "c1", 500, status="error")
        assert tokens_spent_for_change(db_session, "c1") == 500


class TestEnforceBudget:
    def test_zero_budget_means_unlimited(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "llm_token_budget_per_change", 0)
        _add_job(db_session, "c1", 999_999_999)
        enforce_budget(db_session, "c1")  # must not raise

    def test_under_budget_passes_silently(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "llm_token_budget_per_change", 1000)
        _add_job(db_session, "c1", 100)
        enforce_budget(db_session, "c1")  # must not raise

    def test_at_or_over_budget_raises(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "llm_token_budget_per_change", 1000)
        _add_job(db_session, "c1", 1000)
        with pytest.raises(TokenBudgetExceeded, match="already spent"):
            enforce_budget(db_session, "c1")

    def test_warn_threshold_logs_but_does_not_raise(self, db_session, monkeypatch, caplog):
        monkeypatch.setattr(settings, "llm_token_budget_per_change", 1000)
        _add_job(db_session, "c1", 850)  # 85% > default warn_at=0.8
        import logging
        with caplog.at_level(logging.WARNING, logger="app.core.llm_budget"):
            enforce_budget(db_session, "c1")  # must not raise
        assert any("of its LLM budget" in r.message for r in caplog.records)
