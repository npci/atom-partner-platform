# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the structured error taxonomy (Finding 15:
security_architecture_skills.md §5.3/§14.4, EA_Skills.md P8)."""
from app.core.errors import (
    GitLabIntegrationError,
    LlmBudgetExceededError,
    LlmProviderError,
    NpciDeliveryError,
    PartnerPlatformError,
    ReviewConvergenceError,
    SecurityValidationError,
    classify,
)
from app.core.llm_budget import TokenBudgetExceeded
from app.core.resilience import CircuitOpenError


class TestPartnerPlatformErrorSubclasses:
    def test_each_subclass_has_its_own_category_and_code(self):
        cases = [
            (LlmProviderError, "technical", "llm_provider_error"),
            (LlmBudgetExceededError, "business", "llm_budget_exceeded"),
            (GitLabIntegrationError, "resource_access", "gitlab_integration_error"),
            (SecurityValidationError, "security", "security_validation_failed"),
            (ReviewConvergenceError, "business", "review_did_not_converge"),
            (NpciDeliveryError, "resource_access", "npci_delivery_failed"),
        ]
        for cls, category, code in cases:
            exc = cls("message")
            assert exc.category == category
            assert exc.code == code

    def test_code_override(self):
        exc = LlmProviderError("boom", code="custom_code")
        assert exc.code == "custom_code"
        assert exc.category == "technical"  # category unaffected by code override

    def test_token_budget_exceeded_is_classified_as_business(self):
        exc = TokenBudgetExceeded("budget exhausted")
        assert isinstance(exc, LlmBudgetExceededError)
        assert exc.category == "business"
        assert exc.code == "llm_budget_exceeded"


class TestClassify:
    def test_classifies_partner_platform_error_subclass_directly(self):
        category, code = classify(GitLabIntegrationError("boom"))
        assert (category, code) == ("resource_access", "gitlab_integration_error")

    def test_classifies_token_budget_exceeded_as_business(self):
        category, code = classify(TokenBudgetExceeded("boom"))
        assert (category, code) == ("business", "llm_budget_exceeded")

    def test_classifies_circuit_open_error_by_name_heuristic(self):
        category, code = classify(CircuitOpenError("circuit open"))
        assert (category, code) == ("technical", "circuit_open")

    def test_classifies_timeout_error_by_name_heuristic(self):
        category, code = classify(TimeoutError("timed out"))
        assert (category, code) == ("resource_access", "dependency_unavailable")

    def test_classifies_connection_error_by_name_heuristic(self):
        category, code = classify(ConnectionError("connection refused"))
        assert (category, code) == ("resource_access", "dependency_unavailable")

    def test_unknown_exception_falls_back_to_unclassified(self):
        category, code = classify(ValueError("some random error"))
        assert (category, code) == ("technical", "unclassified_error")

    def test_bare_runtime_error_falls_back_to_unclassified(self):
        category, code = classify(RuntimeError("generic failure"))
        assert (category, code) == ("technical", "unclassified_error")
