# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Every boundary declared in `core/hostility.py` must actually be ENFORCED
somewhere in the application.

This guard exists because the same defect occurred three separate times:
`agent_job_dispatch`, `ollama_embed`, and `gitlab_api` were each declared in
the registry — with tiers, timeouts, breaker thresholds and bulkhead caps —
while nothing in the codebase ever looked them up. The registry read like
configuration and was, for half its entries, decoration: an operator tuning
`bulkhead_max_concurrent` for a boundary would see no effect whatsoever, and
nothing failed to warn them.

Each instance was found by hand, one at a time, months apart. This test turns
"someone remembers to audit the registry" into something CI enforces, so
adding a boundary without wiring it fails immediately rather than sitting
unnoticed until a live incident.

Deliberately a static reference scan rather than a behavioural test: the six
boundaries are enforced through genuinely different mechanisms (ASGI
middleware, a context-manager breaker, a client-constructor timeout, a
semaphore around a background job), so there is no single runtime hook they
all pass through. What CAN be asserted uniformly is that each declared name is
consumed by non-registry code — which is exactly the property that was false.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.hostility import BOUNDARIES

APP_DIR = Path(__file__).resolve().parent.parent / "app"
REGISTRY_FILE = APP_DIR / "core" / "hostility.py"


def _application_sources() -> list[Path]:
    """Every application .py file except the registry that declares the
    boundaries (which naturally mentions all of them) and __pycache__."""
    return [
        p for p in APP_DIR.rglob("*.py")
        if p != REGISTRY_FILE and "__pycache__" not in p.parts
    ]


def _files_referencing(name: str) -> list[str]:
    pattern = re.compile(rf"""["']{re.escape(name)}["']""")
    hits = []
    for path in _application_sources():
        try:
            if pattern.search(path.read_text(encoding="utf-8")):
                hits.append(str(path.relative_to(APP_DIR)))
        except (OSError, UnicodeDecodeError):
            continue
    return hits


@pytest.mark.parametrize("boundary_name", sorted(BOUNDARIES))
def test_declared_boundary_is_referenced_by_application_code(boundary_name):
    """A boundary nobody looks up is configuration theatre — its limits are
    displayed, documented, and never applied."""
    refs = _files_referencing(boundary_name)
    assert refs, (
        f"boundary {boundary_name!r} is declared in core/hostility.py but no "
        f"application code references it, so its tier/timeout/breaker/bulkhead "
        f"limits are never enforced. Either wire it up (see "
        f"rag/code_ingestion.py::_gitlab_project or core/llm.py for the two "
        f"patterns) or remove it from the registry — do not leave a limit that "
        f"looks configured but does nothing."
    )


def test_every_boundary_is_covered_by_this_guard():
    """Guards against the registry growing an entry that this test silently
    skips (e.g. if BOUNDARIES were ever built lazily or filtered)."""
    expected = {
        "a2a_inbound",
        "npci_a2a_outbound",
        "llm_provider",
        "gitlab_api",
        "ollama_embed",
        "agent_job_dispatch",
    }
    assert set(BOUNDARIES) == expected, (
        "the boundary set changed — add the new boundary to this list AND make "
        "sure it is actually enforced, then update docs/SECURITY_ARCHITECTURE.md "
        "§6's hostility-tier table"
    )


class TestGitlabBoundaryWiring:
    """`gitlab_api` was the last of the three unenforced boundaries. These pin
    the specific mechanism, since a reference alone would satisfy the scan
    above without necessarily bounding anything."""

    def test_client_is_constructed_with_an_explicit_timeout(self):
        """python-gitlab's timeout defaults to None — no timeout at all — and
        it propagates to every call made through the returned handle, so this
        is the only control that reaches `repository_tree`, `files.get`, etc."""
        src = (APP_DIR / "rag" / "code_ingestion.py").read_text(encoding="utf-8")
        assert "timeout=(limits.timeout_connect_s, limits.timeout_read_s)" in src

    def test_handshake_is_wrapped_in_breaker_and_bulkhead(self):
        src = (APP_DIR / "rag" / "code_ingestion.py").read_text(encoding="utf-8")
        assert 'breaker_for("gitlab_api")' in src
        assert 'bulkhead_for("gitlab_api")' in src

    def test_limits_come_from_the_registry_not_hardcoded(self):
        """The point of ADR-0004 is one source of truth; a literal here would
        drift from the registry silently."""
        src = (APP_DIR / "rag" / "code_ingestion.py").read_text(encoding="utf-8")
        assert 'get_boundary("gitlab_api")' in src


class TestGitlabResilienceBehaviour:
    """Runtime proof, not just a source scan: build the handle against a fake
    python-gitlab and assert the limits are really applied."""

    @staticmethod
    def _install_fake_gitlab(monkeypatch, *, fail: bool = False, calls: dict | None = None):
        import sys
        import types

        mod = types.ModuleType("gitlab")

        class _Projects:
            def get(self, repo_id):
                if calls is not None:
                    calls["n"] = calls.get("n", 0) + 1
                if fail:
                    raise RuntimeError("gitlab unreachable")
                return f"project:{repo_id}"

        class Gitlab:
            def __init__(self, base, private_token=None, keep_base_url=False, timeout=None):
                self.timeout = timeout
                self.projects = _Projects()

        mod.Gitlab = Gitlab
        monkeypatch.setitem(sys.modules, "gitlab", mod)
        return mod

    @staticmethod
    def _repo():
        from app.models import CodeRepo
        return CodeRepo(gitlab_url="https://gitlab.example", gitlab_repo="grp/proj")

    def test_timeout_tuple_is_passed_to_the_client(self, monkeypatch):
        from app.core import resilience
        from app.core.hostility import get as get_boundary
        from app.rag import code_ingestion

        resilience.reset_for_tests()
        captured = {}
        mod = self._install_fake_gitlab(monkeypatch)
        original = mod.Gitlab

        def _capture(*a, **kw):
            captured.update(kw)
            return original(*a, **kw)

        mod.Gitlab = _capture

        code_ingestion._gitlab_project(self._repo(), "tok")
        limits = get_boundary("gitlab_api")
        assert captured["timeout"] == (limits.timeout_connect_s, limits.timeout_read_s)

    def test_circuit_opens_after_repeated_failures_and_stops_calling(self, monkeypatch):
        """Once GitLab is known down, an indexing run must fail fast instead of
        paying the full read timeout on every remaining call."""
        import pytest as _pytest

        from app.core import resilience
        from app.rag import code_ingestion

        resilience.reset_for_tests()
        calls = {}
        self._install_fake_gitlab(monkeypatch, fail=True, calls=calls)

        threshold = resilience.breaker_for("gitlab_api").failure_threshold
        for _ in range(threshold):
            with _pytest.raises(Exception):
                code_ingestion._gitlab_project(self._repo(), "tok")

        assert resilience.breaker_for("gitlab_api").state == "open"
        calls_before = calls["n"]

        with _pytest.raises(resilience.CircuitOpenError):
            code_ingestion._gitlab_project(self._repo(), "tok")
        assert calls["n"] == calls_before, "an open circuit still hit the network"
        resilience.reset_for_tests()

    def test_success_path_returns_the_project_handle(self, monkeypatch):
        from app.core import resilience
        from app.rag import code_ingestion

        resilience.reset_for_tests()
        self._install_fake_gitlab(monkeypatch)
        assert code_ingestion._gitlab_project(self._repo(), "tok") == "project:grp/proj"
