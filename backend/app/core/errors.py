# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Structured error taxonomy — security_architecture_skills.md §5.3/§14.4,
EA_Skills.md P8 (clear business vs. technical error taxonomy).

See docs/ARCHITECTURE_REVIEW_ACTIONS.md Finding 15. Designed to be adopted
incrementally: a small set of `PartnerPlatformError` subclasses for the
highest-value raise sites (LLM, GitLab, review convergence, budget), plus a
best-effort `classify()` heuristic for every OTHER exception so
`api/dashboard/jobs.py::_run_job` can always stamp a category/code onto a
failed job, even for exceptions this module doesn't know about by name.
"""
from __future__ import annotations


class PartnerPlatformError(Exception):
    """Base for all taxonomy-mapped errors. `code` is a stable, machine-
    readable identifier; `category` groups it for telemetry/alerting.

    Categories (mirrors EA_Skills.md's failure taxonomy):
      business         — a rule/policy/budget was hit; not a bug
      technical         — an unexpected internal condition
      resource_access   — a dependency (DB, LLM provider, GitLab, NPCI) failed
      security          — a validation/authz/integrity check failed
    """
    code: str = "unknown_error"
    category: str = "technical"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class LlmProviderError(PartnerPlatformError):
    category = "technical"
    code = "llm_provider_error"


class LlmBudgetExceededError(PartnerPlatformError):
    category = "business"
    code = "llm_budget_exceeded"


class GitLabIntegrationError(PartnerPlatformError):
    category = "resource_access"
    code = "gitlab_integration_error"


class SecurityValidationError(PartnerPlatformError):
    category = "security"
    code = "security_validation_failed"


class ReviewConvergenceError(PartnerPlatformError):
    category = "business"
    code = "review_did_not_converge"


class NpciDeliveryError(PartnerPlatformError):
    category = "resource_access"
    code = "npci_delivery_failed"


# Heuristic classification for exceptions this module doesn't know about by
# type. Checked in order — the first matching rule wins. Kept as a small,
# explicit, ordered list (not a dict) so priority is obvious at a glance:
# a `TimeoutError` that also happens to have "circuit" in some wrapper's name
# should still classify as a timeout, not a circuit-open, if that's the more
# specific/accurate signal for a given exception instance.
_NAME_HEURISTICS: list[tuple[str, str, str]] = [
    ("circuitopen", "technical", "circuit_open"),
    ("timeout", "resource_access", "dependency_unavailable"),
    ("connectionerror", "resource_access", "dependency_unavailable"),
    ("connecterror", "resource_access", "dependency_unavailable"),
]


def classify(exc: Exception) -> tuple[str, str]:
    """Best-effort classification for exceptions NOT already raised as a
    PartnerPlatformError subclass — used at job-runner catch sites so the
    UI/telemetry gets a category even for a third-party exception."""
    if isinstance(exc, PartnerPlatformError):
        return exc.category, exc.code
    name = type(exc).__name__.lower()
    for needle, category, code in _NAME_HEURISTICS:
        if needle in name:
            return category, code
    return "technical", "unclassified_error"


def safe_exc(exc: Exception) -> str:
    """The exception's CLASS NAME only — never `str(exc)`.

    Third-party exception messages are not ours to control: a redis-py error
    embeds the connection URL (which carries the password), SQLAlchemy echoes
    the failing statement, httpx echoes the resolved host, and OSError echoes
    absolute server paths. Interpolating any of those into a log line — or
    worse, into an API response — is CWE-209 (Information Exposure Through an
    Error Message), flagged across `api/dashboard/jobs.py`,
    `a2a_common/hmac_signer.py`, `a2a_common/handlers/change_communication.py`
    and `npci_client.py`.

    The class name is the actionable half of an exception (`TimeoutError` vs.
    `AuthenticationError` tells you where to look) and is a fixed symbol from
    the library's source, never attacker-influenced content. Pair this with a
    companion `logger.debug(..., exc_info=True)` where the full message and
    traceback are genuinely wanted: DEBUG is off in production by default
    (see `main.py`), so detail is available on demand rather than by accident.

    For messages WE raise ourselves, `PartnerPlatformError.code` is the stable
    identifier to surface instead — see `classify()`.
    """
    return type(exc).__name__


def user_facing_error(exc: Exception) -> str:
    """The string to persist on a row the UI renders (`AgentJob.error`,
    `CodeRepo.last_error`, `OutboundA2ARetry.last_error`).

    Allowlist, deliberately: exceptions WE raise carry author-written guidance
    that is the whole point of showing an error at all ("no GitLab token
    configured (set one in Settings)"), so `PartnerPlatformError`,
    `RuntimeError` and `ValueError` pass through. Every other type is library
    text we don't control and collapses to the stable `category: code` pair.

    A blocklist was rejected: it fails open, so each new dependency's
    exception type leaks by default until someone remembers to add it.

    Full detail is never lost — catch sites log `exc_info=True` alongside, and
    operators join back to it on job_id / correlation_id.
    """
    if isinstance(exc, (PartnerPlatformError, RuntimeError, ValueError)):
        return str(exc)[:500]
    category, code = classify(exc)
    return f"{category}: {code}"
