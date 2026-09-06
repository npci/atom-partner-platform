# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""Tests for the cross-module symbol-usage check (SDLC Gap 2:
docs/ARCHITECTURE_REVIEW_ACTIONS.md, Tier 3)."""
from app.models import CodeRepo
from app.rag import symbol_usage


def _repo() -> CodeRepo:
    return CodeRepo(
        id="r1", label="test", gitlab_repo="group/proj", gitlab_branch="main",
    )


class TestFindSymbolUsages:
    def test_returns_empty_without_token(self, db_session, monkeypatch):
        monkeypatch.setattr(symbol_usage, "_gitlab_token", lambda db: None)
        out = symbol_usage.find_symbol_usages(db_session, _repo(), ["PaymentRouter"])
        assert out == {}

    def test_returns_empty_with_no_symbols(self, db_session, monkeypatch):
        monkeypatch.setattr(symbol_usage, "_gitlab_token", lambda db: "tok")
        out = symbol_usage.find_symbol_usages(db_session, _repo(), [])
        assert out == {}

    def test_returns_empty_when_project_handle_fails(self, db_session, monkeypatch):
        monkeypatch.setattr(symbol_usage, "_gitlab_token", lambda db: "tok")

        def _fail(*a, **kw):
            raise RuntimeError("gitlab unreachable")

        monkeypatch.setattr(symbol_usage, "_gitlab_project", _fail)
        out = symbol_usage.find_symbol_usages(db_session, _repo(), ["PaymentRouter"])
        assert out == {}

    def test_aggregates_search_results_per_symbol(self, db_session, monkeypatch):
        monkeypatch.setattr(symbol_usage, "_gitlab_token", lambda db: "tok")

        class _FakeProject:
            def search(self, scope, query):
                return [{"path": f"src/{query}Consumer.java"}, {"path": f"src/{query}Test.java"}]

        monkeypatch.setattr(symbol_usage, "_gitlab_project", lambda repo, token: _FakeProject())
        out = symbol_usage.find_symbol_usages(db_session, _repo(), ["PaymentRouter"])
        assert out == {"PaymentRouter": ["src/PaymentRouterConsumer.java", "src/PaymentRouterTest.java"]}

    def test_symbol_with_no_hits_is_omitted(self, db_session, monkeypatch):
        monkeypatch.setattr(symbol_usage, "_gitlab_token", lambda db: "tok")

        class _FakeProject:
            def search(self, scope, query):
                return []

        monkeypatch.setattr(symbol_usage, "_gitlab_project", lambda repo, token: _FakeProject())
        out = symbol_usage.find_symbol_usages(db_session, _repo(), ["Unused"])
        assert out == {}

    def test_one_symbol_search_failure_does_not_abort_the_rest(self, db_session, monkeypatch):
        monkeypatch.setattr(symbol_usage, "_gitlab_token", lambda db: "tok")

        class _FakeProject:
            def search(self, scope, query):
                if query == "Broken":
                    raise RuntimeError("rate limited")
                return [{"path": f"src/{query}.java"}]

        monkeypatch.setattr(symbol_usage, "_gitlab_project", lambda repo, token: _FakeProject())
        out = symbol_usage.find_symbol_usages(db_session, _repo(), ["Broken", "Working"])
        assert out == {"Working": ["src/Working.java"]}

    def test_filters_short_symbols(self, db_session, monkeypatch):
        monkeypatch.setattr(symbol_usage, "_gitlab_token", lambda db: "tok")
        calls = []

        class _FakeProject:
            def search(self, scope, query):
                calls.append(query)
                return [{"path": "src/x.java"}]

        monkeypatch.setattr(symbol_usage, "_gitlab_project", lambda repo, token: _FakeProject())
        symbol_usage.find_symbol_usages(db_session, _repo(), ["ab", "ValidName"])
        assert calls == ["ValidName"]  # "ab" is below _MIN_SYMBOL_LEN

    def test_deduplicates_case_insensitively(self, db_session, monkeypatch):
        monkeypatch.setattr(symbol_usage, "_gitlab_token", lambda db: "tok")
        calls = []

        class _FakeProject:
            def search(self, scope, query):
                calls.append(query)
                return []

        monkeypatch.setattr(symbol_usage, "_gitlab_project", lambda repo, token: _FakeProject())
        symbol_usage.find_symbol_usages(db_session, _repo(), ["PaymentRouter", "paymentrouter"])
        assert calls == ["PaymentRouter"]

    def test_caps_total_symbols_searched(self, db_session, monkeypatch):
        monkeypatch.setattr(symbol_usage, "_gitlab_token", lambda db: "tok")
        calls = []

        class _FakeProject:
            def search(self, scope, query):
                calls.append(query)
                return []

        monkeypatch.setattr(symbol_usage, "_gitlab_project", lambda repo, token: _FakeProject())
        many_symbols = [f"Symbol{i}" for i in range(50)]
        symbol_usage.find_symbol_usages(db_session, _repo(), many_symbols)
        assert len(calls) == symbol_usage._MAX_SYMBOLS_SEARCHED

    def test_respects_max_hits_per_symbol(self, db_session, monkeypatch):
        monkeypatch.setattr(symbol_usage, "_gitlab_token", lambda db: "tok")

        class _FakeProject:
            def search(self, scope, query):
                return [{"path": f"src/f{i}.java"} for i in range(30)]

        monkeypatch.setattr(symbol_usage, "_gitlab_project", lambda repo, token: _FakeProject())
        out = symbol_usage.find_symbol_usages(db_session, _repo(), ["Sym"], max_hits_per_symbol=5)
        assert len(out["Sym"]) == 5


class TestFormatUsageContext:
    def test_empty_map_returns_empty_string(self):
        assert symbol_usage.format_usage_context({}) == ""

    def test_renders_symbols_and_paths(self):
        text = symbol_usage.format_usage_context({"PaymentRouter": ["a.java", "b.java"]})
        assert "PaymentRouter" in text
        assert "a.java" in text
        assert "b.java" in text
