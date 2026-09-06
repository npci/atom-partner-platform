# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Per-change LLM token budget guard.

security_architecture_skills.md §4.2 (bulkhead limits — cost is a bounded
resource), EA_Skills.md P6 (cost-aware data access) / P10 (responsible
resource management). See docs/adr/ARCHITECTURE_REVIEW_ACTIONS.md Finding 4.

Accounting is done at the `AgentJob` level rather than `AgentRun`: every
LLM-touching job (design/code/testing analyse, codegen, review, fix, mr) is
dispatched through exactly one choke point (`api/dashboard/jobs.py::_run_job`),
which wraps the runner in `core.llm.track_token_usage()` and stamps the
resulting total onto the job row. This captures whole-file generation
(`agents/code_files.py`), which issues MANY call_llm() invocations per job and
never writes an `AgentRun` audit row — summing `AgentRun.result_payload`
alone would silently miss the single largest source of spend on this
platform's own admission (code_files.py's batched, 32k-64k-token-per-call
generation passes).
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import LlmBudgetExceededError

logger = logging.getLogger(__name__)


class TokenBudgetExceeded(LlmBudgetExceededError):
    """Raised by enforce_budget() when a change has exhausted its per-change
    LLM token budget. Subclasses the taxonomy's LlmBudgetExceededError
    (Finding 15: security_architecture_skills.md §5.3/§14.4) so a
    TokenBudgetExceeded raised from WITHIN a job runner (rather than at
    dispatch time via start_job(), which converts it to an HTTP 429) is
    still correctly classified as category='business', code='llm_budget_exceeded'
    by core.errors.classify(), instead of falling back to the generic
    'unclassified_error' heuristic."""


def tokens_spent_for_change(db: Session, change_id: str) -> int:
    """Sum of `AgentJob.tokens_used` across every job ever run for this
    change (any kind, any status — a failed job may still have burned tokens
    before it failed)."""
    from app.models import AgentJob
    total = db.execute(
        select(func.coalesce(func.sum(AgentJob.tokens_used), 0))
        .where(AgentJob.change_id == change_id)
    ).scalar_one()
    return int(total or 0)


def enforce_budget(db: Session, change_id: str, *, warn_at: float = 0.8) -> None:
    """Raises TokenBudgetExceeded if the change has already exceeded its
    configured per-change token budget; logs a warning at `warn_at` (default
    80%) of the budget. Call this at the START of a job runner — see
    `api/dashboard/jobs.py::_run_job` — before invoking the agent, so a
    non-converging loop is stopped BEFORE spending more, not just reported
    after the fact."""
    from app.config import settings
    budget = settings.llm_token_budget_per_change
    if budget <= 0:
        return  # 0 = unlimited, explicit opt-out
    spent = tokens_spent_for_change(db, change_id)
    if spent >= budget:
        # Structured, alertable event (security_architecture_skills.md
        # §13.2/§13.3). Budget exhaustion was previously visible only as an
        # HTTP 429 to whoever clicked the button — but it is a cost-control
        # AND abuse signal (a runaway loop, or a change being driven far past
        # its expected spend), so it needs to reach ops, not just the UI.
        from app.core.security_events import emit_security_event
        emit_security_event(
            event_name="llm_budget_exceeded",
            severity="medium",
            boundary="llm_provider",
            decision="rejected",
            reason_code=f"spent={spent} budget={budget}",
            correlation_id=change_id,
        )
        raise TokenBudgetExceeded(
            f"change {change_id} has already spent {spent} tokens, at or "
            f"beyond its budget of {budget}. An operator must raise "
            f"LLM_TOKEN_BUDGET_PER_CHANGE or investigate why this change is "
            f"not converging before further agent runs are permitted."
        )
    if spent >= budget * warn_at:
        logger.warning(
            "change %s has spent %d/%d tokens (%.0f%%) of its LLM budget",
            change_id, spent, budget, 100 * spent / budget,
        )
